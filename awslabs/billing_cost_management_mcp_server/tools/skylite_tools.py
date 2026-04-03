# Copyright Skylite Tek GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# Additions to awslabs/billing-cost-management-mcp-server.
# Upstream: https://github.com/awslabs/mcp/tree/main/src/billing-cost-management-mcp-server
#
# Custom tools:
#   - get_cost_by_tag       NEW: cost filtered by any resource tag + Net metric
#   - get_monthly_costs     costs by service, N months back
#   - get_credits_analysis  credits applied per month + current month by service
#   - read_credits_from_cur credit detail from CUR S3 export
#   - get_invoice_detail    cost breakdown for a specific month
#   - list_aws_invoices     list invoice IDs with amounts
#   - get_invoice_pdf_url   pre-signed download URL for an invoice PDF
#   - download_invoice_pdf  download invoice PDF to local file
#   - get_cost_by_account   costs grouped by linked account

"""Skylite custom billing tools — extends awslabs billing-cost-management-mcp-server."""

import csv
import gzip
import io
import os
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

import boto3
from dateutil.relativedelta import relativedelta
from fastmcp import FastMCP
from pydantic import Field


def _ce():
    return boto3.Session().client('ce', region_name='us-east-1')


def _invoicing():
    return boto3.Session().client('invoicing', region_name='us-east-1')


def _s3():
    return boto3.Session().client('s3', region_name='us-east-1')


def register_skylite_tools(app: FastMCP) -> None:
    """Register all Skylite custom tools onto a FastMCP app instance."""

    @app.tool(
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def get_cost_by_tag(
        tag_key: Annotated[str, Field(description='Tag key to filter on, e.g. "Project"')],
        tag_value: Annotated[str, Field(description='Tag value to filter on, e.g. "LexusES"')],
        start_date: Annotated[
            str,
            Field(
                description='Start date YYYY-MM-DD. Tag must be active as cost allocation tag first.'
            ),
        ],
        end_date: Annotated[
            str,
            Field(description='End date YYYY-MM-DD (exclusive)'),
        ],
        granularity: Annotated[
            str,
            Field(description='DAILY or MONTHLY', default='DAILY'),
        ] = 'DAILY',
        group_by_service: Annotated[
            bool,
            Field(description='Break down by AWS service', default=True),
        ] = True,
    ) -> str:
        """Get AWS costs filtered by a resource tag. Returns both gross (UnblendedCost) and
        net-of-credits (NetUnblendedCost) amounts. Tag must be activated as a cost allocation tag
        in Billing console first — data is only available from the activation date forward."""
        try:
            params = dict(
                TimePeriod={'Start': start_date, 'End': end_date},
                Granularity=granularity,
                Metrics=['UnblendedCost', 'NetUnblendedCost'],
                Filter={'Tags': {'Key': tag_key, 'Values': [tag_value]}},
            )
            if group_by_service:
                params['GroupBy'] = [{'Type': 'DIMENSION', 'Key': 'SERVICE'}]

            resp = _ce().get_cost_and_usage(**params)

            lines = [
                f'AWS Costs — tag {tag_key}={tag_value}',
                f'Period: {start_date} → {end_date} | Granularity: {granularity}',
                '=' * 70,
            ]
            total_ub = 0.0
            total_net = 0.0

            for r in resp['ResultsByTime']:
                day = r['TimePeriod']['Start']
                estimated = ' (estimated)' if r.get('Estimated') else ''
                day_ub = 0.0
                day_net = 0.0
                if r.get('Groups'):
                    for g in sorted(
                        r['Groups'],
                        key=lambda x: -float(x['Metrics']['UnblendedCost']['Amount']),
                    ):
                        svc = g['Keys'][0]
                        ub = float(g['Metrics']['UnblendedCost']['Amount'])
                        net = float(g['Metrics']['NetUnblendedCost']['Amount'])
                        if ub > 0.001:
                            lines.append(
                                f'  {day}{estimated}  {svc:<48}  Gross: ${ub:8.2f}  Net: ${net:8.2f}'
                            )
                        day_ub += ub
                        day_net += net
                    if day_ub > 0.001:
                        lines.append(
                            f'  {day}  {"--- DAY TOTAL ---":<48}  Gross: ${day_ub:8.2f}  Net: ${day_net:8.2f}'
                        )
                        lines.append('')
                else:
                    if not r.get('Total') or float(r['Total'].get('UnblendedCost', {}).get('Amount', 0)) < 0.001:
                        lines.append(f'  {day}{estimated}  (no tagged costs)')
                    else:
                        ub = float(r['Total']['UnblendedCost']['Amount'])
                        net = float(r['Total']['NetUnblendedCost']['Amount'])
                        lines.append(f'  {day}{estimated}  Gross: ${ub:8.2f}  Net: ${net:8.2f}')
                        day_ub, day_net = ub, net

                total_ub += day_ub
                total_net += day_net

            lines.extend([
                '=' * 70,
                f'TOTAL Gross (before credits):  ${total_ub:.2f}',
                f'TOTAL Net   (after credits):   ${total_net:.2f}',
                f'Credits applied:               ${total_ub - total_net:.2f}',
                '',
                'NOTE: If all values are $0, the tag may not be activated as a cost allocation',
                'tag yet, or was activated after the requested period.',
                'Check: https://us-east-1.console.aws.amazon.com/costmanagement/home#/tags',
            ])
            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'

    @app.tool(
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def get_monthly_costs(
        months_back: Annotated[
            int,
            Field(description='How many months back to show (default 12, max 14)', default=12),
        ] = 12,
    ) -> str:
        """Get monthly AWS costs broken down by service. Returns USD amounts."""
        try:
            months_back = min(months_back, 14)
            end = date.today().replace(day=1)
            start = end - relativedelta(months=months_back)

            resp = _ce().get_cost_and_usage(
                TimePeriod={'Start': start.isoformat(), 'End': end.isoformat()},
                Granularity='MONTHLY',
                Metrics=['UnblendedCost'],
                GroupBy=[{'Type': 'DIMENSION', 'Key': 'SERVICE'}],
            )

            lines = ['Monthly AWS Costs (USD)\n' + '=' * 50]
            grand_total = 0.0
            for month_data in resp['ResultsByTime']:
                period = month_data['TimePeriod']['Start'][:7]
                groups = sorted(
                    month_data['Groups'],
                    key=lambda x: float(x['Metrics']['UnblendedCost']['Amount']),
                    reverse=True,
                )
                total = sum(float(g['Metrics']['UnblendedCost']['Amount']) for g in groups)
                grand_total += total
                lines.append(f'\n{period}  |  TOTAL: ${total:,.2f}')
                for g in groups:
                    amt = float(g['Metrics']['UnblendedCost']['Amount'])
                    if amt >= 0.01:
                        lines.append(f'  {g["Keys"][0]:<52} ${amt:>10,.2f}')

            lines.append(f'\n{"=" * 50}')
            lines.append(f'GRAND TOTAL ({months_back} months): ${grand_total:,.2f}')
            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'

    @app.tool(
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def get_credits_analysis(
        months_back: Annotated[
            int,
            Field(description='How many months back to analyze (default 6)', default=6),
        ] = 6,
    ) -> str:
        """Show AWS credits applied per month and breakdown by service for the current month.
        Reveals what the credits are covering (e.g. Bedrock, EC2, FSx)."""
        try:
            ce = _ce()
            end = date.today().replace(day=1)
            start = end - relativedelta(months=months_back)

            monthly = ce.get_cost_and_usage(
                TimePeriod={'Start': start.isoformat(), 'End': date.today().isoformat()},
                Granularity='MONTHLY',
                Metrics=['UnblendedCost'],
                Filter={'Dimensions': {'Key': 'RECORD_TYPE', 'Values': ['Credit']}},
            )

            lines = ['AWS Credits Analysis\n' + '=' * 60]
            lines.append('\nMONTHLY CREDITS APPLIED (negative = credit received):')
            grand_total = 0.0
            for month in monthly['ResultsByTime']:
                period = month['TimePeriod']['Start'][:7]
                amt = float(month['Total']['UnblendedCost']['Amount'])
                if abs(amt) > 0.01:
                    lines.append(f'  {period}  ${amt:,.2f}')
                    grand_total += amt
            lines.append(f'\n  TOTAL credits received: ${grand_total:,.2f}')

            cur_start = date.today().replace(day=1)
            if cur_start < end:
                cur_start = end - relativedelta(months=1)

            by_svc = ce.get_cost_and_usage(
                TimePeriod={'Start': cur_start.isoformat(), 'End': date.today().isoformat()},
                Granularity='MONTHLY',
                Metrics=['UnblendedCost'],
                Filter={'Dimensions': {'Key': 'RECORD_TYPE', 'Values': ['Credit']}},
                GroupBy=[{'Type': 'DIMENSION', 'Key': 'SERVICE'}],
            )
            lines.append(f'\nCURRENT MONTH CREDITS BY SERVICE ({cur_start}):')
            lines.append('-' * 60)
            for month in by_svc['ResultsByTime']:
                groups = sorted(
                    month['Groups'],
                    key=lambda x: float(x['Metrics']['UnblendedCost']['Amount']),
                )
                for g in groups:
                    amt = float(g['Metrics']['UnblendedCost']['Amount'])
                    if abs(amt) > 0.01:
                        lines.append(f'  {g["Keys"][0]:<52} ${amt:>10,.2f}')

            lines.extend([
                '\nNOTE: Credit name, expiration date, and remaining balance',
                'are NOT available via AWS API. Check AWS Console:',
                'https://us-east-1.console.aws.amazon.com/billing/home#/credits',
            ])
            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'

    @app.tool(
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def read_credits_from_cur(
        bucket: Annotated[
            str,
            Field(
                description='S3 bucket name for CUR export (default: skylitetek-cur-exports)',
                default='skylitetek-cur-exports',
            ),
        ] = 'skylitetek-cur-exports',
    ) -> str:
        """Read credit details (name, description, amount) from the CUR S3 export.
        Returns credit names and amounts. Data available ~24h after export creation."""
        try:
            s3 = _s3()
            try:
                objects = s3.list_objects_v2(Bucket=bucket, Prefix='credits/')
            except Exception as e:
                return f'S3 error: {e}'

            files = [
                o['Key']
                for o in objects.get('Contents', [])
                if o['Key'].endswith('.gz') or o['Key'].endswith('.csv')
            ]
            if not files:
                return (
                    'No CUR data files yet in S3. Export was created today — check back in 24h.\n'
                    f'Bucket: s3://{bucket}/credits/\n'
                    'Export: skylitetek-cur-credits (HEALTHY)'
                )

            credits: dict = {}
            for key in files:
                obj = s3.get_object(Bucket=bucket, Key=key)
                raw = obj['Body'].read()
                if key.endswith('.gz'):
                    raw = gzip.decompress(raw)
                reader = csv.DictReader(io.StringIO(raw.decode('utf-8')))
                for row in reader:
                    line_type = row.get('line_item_line_item_type', '')
                    if line_type not in ('Credit', 'Refund'):
                        continue
                    desc = row.get('line_item_line_item_description', 'N/A')
                    product = row.get('line_item_product_code', '')
                    dt = row.get('line_item_usage_start_date', '')[:10]
                    cost = float(row.get('line_item_unblended_cost', 0))
                    k = f'{desc} | {product}'
                    if k not in credits:
                        credits[k] = {'total': 0.0, 'first': dt, 'last': dt, 'type': line_type}
                    credits[k]['total'] += cost
                    if dt < credits[k]['first']:
                        credits[k]['first'] = dt
                    if dt > credits[k]['last']:
                        credits[k]['last'] = dt

            if not credits:
                return 'CUR data found but no Credit/Refund line items yet.'

            lines = [f'AWS Credits from CUR ({len(files)} files)\n' + '=' * 70]
            for desc, info in sorted(credits.items(), key=lambda x: x[1]['total']):
                lines.append(f'\n[{info["type"]}] {desc}')
                lines.append(f'  Period: {info["first"]} \u2192 {info["last"]}')
                lines.append(f'  Total:  ${info["total"]:,.2f}')
            total = sum(c['total'] for c in credits.values())
            lines.append(f'\n{"=" * 70}\nGRAND TOTAL CREDITS: ${total:,.2f}')
            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'

    @app.tool(
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def get_invoice_detail(
        month: Annotated[
            str, Field(description='Month in YYYY-MM format, e.g. 2025-05')
        ],
    ) -> str:
        """Get detailed cost breakdown for a specific month by service and usage type."""
        try:
            year, mon = map(int, month.split('-'))
            start = date(year, mon, 1)
            end = start + relativedelta(months=1)
            ce = _ce()

            resp_svc = ce.get_cost_and_usage(
                TimePeriod={'Start': start.isoformat(), 'End': end.isoformat()},
                Granularity='MONTHLY',
                Metrics=['UnblendedCost', 'UsageQuantity'],
                GroupBy=[{'Type': 'DIMENSION', 'Key': 'SERVICE'}],
            )
            resp_usage = ce.get_cost_and_usage(
                TimePeriod={'Start': start.isoformat(), 'End': end.isoformat()},
                Granularity='MONTHLY',
                Metrics=['UnblendedCost'],
                GroupBy=[{'Type': 'DIMENSION', 'Key': 'USAGE_TYPE'}],
            )

            lines = [f'Invoice Detail: {month}\n' + '=' * 60, '\nBy Service:']
            total = 0.0
            for r in resp_svc['ResultsByTime']:
                for g in sorted(
                    r['Groups'],
                    key=lambda x: -float(x['Metrics']['UnblendedCost']['Amount']),
                ):
                    amt = float(g['Metrics']['UnblendedCost']['Amount'])
                    if amt >= 0.01:
                        lines.append(f'  {g["Keys"][0]:<52} ${amt:>10,.2f}')
                        total += amt
            lines.append(f'\n  TOTAL: ${total:,.2f}')

            lines.append('\nBy Usage Type (top 20):')
            usage_groups = []
            for r in resp_usage['ResultsByTime']:
                for g in r['Groups']:
                    amt = float(g['Metrics']['UnblendedCost']['Amount'])
                    if amt >= 0.01:
                        usage_groups.append((g['Keys'][0], amt))
            for utype, amt in sorted(usage_groups, key=lambda x: -x[1])[:20]:
                lines.append(f'  {utype:<56} ${amt:>10,.4f}')

            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'

    @app.tool(
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def list_aws_invoices(
        year: Annotated[
            Optional[int],
            Field(description='Filter by year (e.g. 2025). Optional.', default=None),
        ] = None,
    ) -> str:
        """List all AWS invoice IDs with dates and amounts. Use this first to get invoice IDs
        before downloading PDFs."""
        try:
            client = _invoicing()
            sts = boto3.Session().client('sts')
            account_id = sts.get_caller_identity()['Account']
            target_year = year or datetime.now().year
            all_invoices = []

            for month in range(1, 13):
                start = datetime(target_year, month, 1)
                end = (
                    datetime(target_year, 12, 31)
                    if month == 12
                    else datetime(target_year, month + 1, 1)
                )
                try:
                    resp = client.list_invoice_summaries(
                        Selector={'ResourceType': 'ACCOUNT_ID', 'Value': account_id},
                        Filter={'TimeInterval': {'StartDate': start, 'EndDate': end}},
                    )
                    all_invoices.extend(resp.get('InvoiceSummaries', []))
                except Exception:
                    continue

            if not all_invoices:
                return f'No invoices found for {target_year}.'

            lines = [f'AWS Invoices for {target_year} ({len(all_invoices)} found)\n' + '=' * 70]
            for inv in all_invoices:
                inv_id = inv.get('InvoiceId', 'N/A')
                issued = str(inv.get('IssuedDate', ''))[:10]
                bp = inv.get('BillingPeriod', {})
                period = f'{bp.get("Year", "")}-{bp.get("Month", 0):02d}'
                entity = inv.get('Entity', {}).get('InvoicingEntity', '')
                pay = inv.get('PaymentCurrencyAmount', {})
                amount = pay.get('TotalAmount', '?')
                currency = pay.get('CurrencyCode', '')
                lines.append(
                    f'  {inv_id:<25} {period}  {issued}  {amount:>12} {currency}  {entity[:30]}'
                )
            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'

    @app.tool(
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def get_invoice_pdf_url(
        invoice_id: Annotated[str, Field(description='The AWS invoice ID')],
    ) -> str:
        """Get a pre-signed download URL for an AWS invoice PDF. Use list_aws_invoices first
        to get the invoice ID."""
        try:
            resp = _invoicing().get_invoice_pdf(InvoiceId=invoice_id)
            pdf = resp.get('InvoicePDF', {})
            url = pdf.get('DocumentUrl', '')
            expires = pdf.get('DocumentUrlExpirationDate', '')
            supplemental = pdf.get('SupplementalDocuments', [])
            lines = [
                f'Invoice PDF: {invoice_id}',
                f'Download URL (expires: {expires}):',
                url,
            ]
            if supplemental:
                lines.append('\nSupplemental documents:')
                for doc in supplemental:
                    lines.append(f'  {doc.get("DocumentUrl", "")}')
            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'

    @app.tool(
        annotations={'readOnlyHint': False},
    )
    async def download_invoice_pdf(
        invoice_id: Annotated[str, Field(description='The AWS invoice ID')],
        output_dir: Annotated[
            str,
            Field(
                description='Directory to save the PDF (default: ~/Downloads)',
                default='~/Downloads',
            ),
        ] = '~/Downloads',
    ) -> str:
        """Download an AWS invoice PDF to a local file. Returns the saved file path."""
        try:
            resp = _invoicing().get_invoice_pdf(InvoiceId=invoice_id)
            pdf = resp.get('InvoicePDF', {})
            url = pdf.get('DocumentUrl', '')
            if not url:
                return 'No download URL returned.'

            out = os.path.expanduser(output_dir)
            os.makedirs(out, exist_ok=True)
            output_path = os.path.join(out, f'aws-invoice-{invoice_id}.pdf')
            urllib.request.urlretrieve(url, output_path)
            size_kb = os.path.getsize(output_path) / 1024

            lines = [
                f'Downloaded: {output_path}',
                f'Size: {size_kb:.1f} KB',
                f'Invoice ID: {invoice_id}',
            ]
            for i, doc in enumerate(pdf.get('SupplementalDocuments', []), 1):
                supp_url = doc.get('DocumentUrl', '')
                supp_path = os.path.join(out, f'aws-invoice-{invoice_id}-supplement-{i}.pdf')
                try:
                    urllib.request.urlretrieve(supp_url, supp_path)
                    lines.append(f'Supplement {i}: {supp_path}')
                except Exception as ex:
                    lines.append(f'Supplement {i} failed: {ex}')
            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'

    @app.tool(
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def get_cost_by_account(
        month: Annotated[str, Field(description='Month in YYYY-MM format')],
    ) -> str:
        """Get AWS costs grouped by linked account for a month."""
        try:
            year, mon = map(int, month.split('-'))
            start = date(year, mon, 1)
            end = start + relativedelta(months=1)

            resp = _ce().get_cost_and_usage(
                TimePeriod={'Start': start.isoformat(), 'End': end.isoformat()},
                Granularity='MONTHLY',
                Metrics=['UnblendedCost'],
                GroupBy=[{'Type': 'DIMENSION', 'Key': 'LINKED_ACCOUNT'}],
            )

            lines = [f'Costs by Account: {month}\n' + '=' * 50]
            total = 0.0
            for r in resp['ResultsByTime']:
                for g in sorted(
                    r['Groups'],
                    key=lambda x: -float(x['Metrics']['UnblendedCost']['Amount']),
                ):
                    amt = float(g['Metrics']['UnblendedCost']['Amount'])
                    if amt >= 0.01:
                        lines.append(f'  {g["Keys"][0]:<20} ${amt:>12,.2f}')
                        total += amt
            lines.append(f'\n  TOTAL: ${total:,.2f}')
            return '\n'.join(lines)
        except Exception as e:
            return f'Error: {e}'
