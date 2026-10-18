"""Create a readable, formula-free XLSX from a trusted portable ledger snapshot.

Uses only the standard library so the HA runtime needs no desktop dependencies.
All source text is serialized as inline strings; formula-like descriptions stay text.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import io
import json
import re
import math
import unicodedata
import zipfile
from xml.sax.saxutils import escape

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_ROWS = 50000
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _text(value: object) -> str:
    return escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value)))


def _column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet(rows: list[list[object]], widths: list[int], *, header: int = 1) -> str:
    out = [f'<worksheet xmlns="{NS}"><sheetViews><sheetView workbookViewId="0">',
           f'<pane ySplit="{header}" topLeftCell="A{header + 1}" activePane="bottomLeft" state="frozen"/>',
           '</sheetView></sheetViews><cols>']
    for index, width in enumerate(widths, 1):
        out.append(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>')
    out.append('</cols><sheetData>')
    for index, row in enumerate(rows, 1):
        lines = max((max(1, math.ceil(sum(2 if unicodedata.east_asian_width(c) in {'W', 'F'} else 1 for c in str(value)) / max(1, widths[col] - 2))) for col, value in enumerate(row)), default=1)
        height = 30 if index == header else min(409, max(28, lines * 16))
        out.append(f'<row r="{index}" ht="{height}" customHeight="1">')
        for col, value in enumerate(row, 1):
            ref = f'{_column(col)}{index}'
            if index == header:
                out.append(f'<c r="{ref}" s="1" t="inlineStr"><is><t>{_text(value)}</t></is></c>')
            elif isinstance(value, Decimal):
                out.append(f'<c r="{ref}" s="2"><v>{value}</v></c>')
            elif isinstance(value, int):
                out.append(f'<c r="{ref}" s="3"><v>{value}</v></c>')
            elif isinstance(value, date):
                serial = (value - date(1899, 12, 30)).days
                out.append(f'<c r="{ref}" s="4"><v>{serial}</v></c>')
            else:
                out.append(f'<c r="{ref}" s="3" t="inlineStr"><is><t xml:space="preserve">{_text(value)}</t></is></c>')
        out.append('</row>')
    out.append('</sheetData>')
    if header == 1 and len(rows) > 1:
        out.append(f'<autoFilter ref="A1:{_column(len(widths))}{len(rows)}"/>')
    out.append('</worksheet>')
    return ''.join(out)


def ledger_workbook(content: bytes) -> bytes:
    """Read one bounded member without extracting archives or executing content."""
    with zipfile.ZipFile(io.BytesIO(content)) as source:
        members = [m for m in source.infolist() if m.filename == 'transactions.csv']
        if len(members) != 1 or not 1 <= members[0].file_size <= MAX_CSV_BYTES:
            raise ValueError('ledger_snapshot_invalid')
        with source.open(members[0]) as stream:
            raw = stream.read(MAX_CSV_BYTES + 1)
        if len(raw) > MAX_CSV_BYTES:
            raise ValueError('ledger_snapshot_too_large')
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    required = {'id', 'kind', 'status', 'amount_cents', 'date', 'vendor', 'description'}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError('ledger_snapshot_invalid')
    rows = []
    for row in reader:
        if len(rows) >= MAX_ROWS:
            raise ValueError('ledger_snapshot_too_large')
        if (None in row or row['kind'] not in {'payment', 'refund'}
                or row['status'] not in {'active', 'void'}):
            raise ValueError('ledger_snapshot_invalid')
        rows.append(row)
    payments = refunds = active = 0
    details: list[list[object]] = [["记录编号", "日期", "类型", "金额（元）", "净支出（元）", "商家", "说明", "分类", "子分类", "费用类型", "标签", "状态", "关联付款", "定金", "作废原因"]]
    for row in sorted(rows, key=lambda r: (r['date'], r['id'])):
        cents = int(row['amount_cents'])
        if cents < 0:
            raise ValueError('ledger_snapshot_invalid')
        signed = cents if row['kind'] == 'payment' else -cents
        valid = row['status'] == 'active'
        if valid:
            active += 1
            payments += cents if row['kind'] == 'payment' else 0
            refunds += cents if row['kind'] == 'refund' else 0
        tags = json.loads(row.get('tags_json') or '[]')
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise ValueError('ledger_snapshot_invalid')
        details.append([row['id'], date.fromisoformat(row['date']), '付款' if signed >= 0 and row['kind'] == 'payment' else '退款',
                        Decimal(cents) / 100, Decimal(signed if valid else 0) / 100,
                        row['vendor'], row['description'], row.get('category', ''), row.get('subcategory', ''),
                        row.get('expense_type', ''), '、'.join(tags), '有效' if valid else '已作废',
                        row.get('payment_id', ''), '是' if row.get('is_deposit') == '1' else '否', row.get('void_reason', '')])
    summary: list[list[object]] = [["装修账本汇总", "金额 / 数量"], ["有效记录（笔）", active],
        ["付款合计（元）", Decimal(payments) / 100], ["退款合计（元）", Decimal(refunds) / 100],
        ["净支出（元）", Decimal(payments - refunds) / 100], ["全部记录（含作废）", len(rows)],
        ["统计口径", "净支出仅计算有效记录，退款扣减；明细保留全部记录与作废标记。"],
        ["数据来源", "Renovation Hub 完整账本导出快照 transactions.csv"]]
    styles = f'''<styleSheet xmlns="{NS}"><numFmts count="2"><numFmt numFmtId="164" formatCode="#,##0.00;[Red](#,##0.00)"/><numFmt numFmtId="165" formatCode="yyyy-mm-dd"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF243B53"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="5"><xf/><xf fontId="1" fillId="2" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="164" applyNumberFormat="1"/><xf applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="165" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as output:
        output.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in (1, 2))+'</Types>')
        output.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        output.writestr('xl/workbook.xml', f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="汇总" sheetId="1" r:id="rId1"/><sheet name="全部明细" sheetId="2" r:id="rId2"/></sheets></workbook>')
        output.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in (1, 2))+'<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        output.writestr('xl/styles.xml', styles)
        output.writestr('xl/worksheets/sheet1.xml', _sheet(summary, [30, 85]))
        output.writestr('xl/worksheets/sheet2.xml', _sheet(details, [14, 14, 10, 16, 18, 24, 56, 20, 20, 18, 42, 12, 14, 10, 28]))
    return target.getvalue()
