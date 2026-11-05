"""
Clutch (ক্ল্যাচ) - Invoice & Courier Slip PDF Generator
Generates clean, professional A4 / thermal PDF invoices using ReportLab.
Includes:
- Merchant brand name & header
- Order identifier & timestamps
- Recipient customer contact & delivery zone details
- Itemized product breakdown table
- Delivery zone fee & total Cash on Delivery (COD) amount
- Dynamically rendered Code128 barcode matching Steadfast tracking code
- Export to designated output directory ('invoices/')
"""

import os
from typing import Optional
from datetime import datetime, timezone

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
)
from reportlab.graphics.barcode import code128

from app.models.order import Order, DeliveryZone

DEFAULT_INVOICE_DIR = "invoices"
CYBER_MAGENTA = colors.HexColor("#FF1493")
OBSIDIAN_DARK = colors.HexColor("#16101D")
SLATE_BORDER = colors.HexColor("#CBD5E1")
LIGHT_BG = colors.HexColor("#F8FAFC")
TEXT_MUTED = colors.HexColor("#64748B")
TEXT_DARK = colors.HexColor("#0F172A")


def get_zone_display_name(zone: DeliveryZone) -> str:
    """Friendly delivery zone string."""
    if zone == DeliveryZone.CORE:
        return "Dhaka Core (৳70)"
    elif zone == DeliveryZone.SUBURB:
        return "Dhaka Suburbs (৳100)"
    elif zone == DeliveryZone.OUTSIDE:
        return "Outside Dhaka (৳130)"
    return str(zone)


def generate_invoice_pdf(
    order: Order,
    output_dir: str = DEFAULT_INVOICE_DIR,
    filename: Optional[str] = None,
    brand_name: str = "Clutch Store",
) -> str:
    """
    Generate an invoice PDF for an order and save it to output_dir.
    
    Args:
        order: Order domain model instance
        output_dir: Directory where invoice will be stored (defaults to 'invoices/')
        filename: Custom filename (defaults to 'invoice_{order_id}.pdf')
        brand_name: Merchant brand name displayed at the top
        
    Returns:
        Absolute filepath to the generated PDF.
    """
    # 1. Ensure directory exists
    os.makedirs(output_dir, exist_ok=True)

    if not filename:
        clean_order_id = order.order_id.replace(" ", "_").replace("/", "-")
        filename = f"invoice_{clean_order_id}.pdf"

    pdf_filepath = os.path.abspath(os.path.join(output_dir, filename))

    # 2. Setup Document Template (A4, 20mm margins)
    doc = SimpleDocTemplate(
        pdf_filepath,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    # Custom typography styles
    style_brand = ParagraphStyle(
        "BrandHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=OBSIDIAN_DARK,
    )
    style_invoice_tag = ParagraphStyle(
        "InvoiceTag",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=CYBER_MAGENTA,
        alignment=2,  # Right-aligned
    )
    style_meta_right = ParagraphStyle(
        "MetaRight",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=TEXT_MUTED,
        alignment=2,
    )
    style_label = ParagraphStyle(
        "LabelText",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=TEXT_DARK,
    )
    style_value = ParagraphStyle(
        "ValueText",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=TEXT_DARK,
    )
    style_table_header = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=colors.white,
    )
    style_table_cell = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=TEXT_DARK,
    )
    style_table_cell_bold = ParagraphStyle(
        "TableCellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=TEXT_DARK,
    )
    style_total_highlight = ParagraphStyle(
        "TotalHighlight",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=CYBER_MAGENTA,
        alignment=2,
    )
    style_footer = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=TEXT_MUTED,
        alignment=1,  # Center
    )

    story = []

    # ---------------------------------------------------------------------
    # Header: Brand & Meta Information
    # ---------------------------------------------------------------------
    created_date_str = order.created_at.strftime("%Y-%m-%d %I:%M %p") if order.created_at else datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p")

    header_data = [
        [
            Paragraph(f"<b>{brand_name}</b>", style_brand),
            Paragraph("INVOICE / COD SLIP", style_invoice_tag),
        ],
        [
            Paragraph("<font color='#64748B'>Automated Order Fulfillment</font>", style_table_cell),
            Paragraph(f"<b>Order ID:</b> {order.order_id}<br/><b>Date:</b> {created_date_str}", style_meta_right),
        ],
    ]
    header_table = Table(header_data, colWidths=[300, 222])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 14))

    # ---------------------------------------------------------------------
    # Barcode & Courier Tracking Code
    # ---------------------------------------------------------------------
    tracking_val = order.tracking_code or order.consignment_id or order.order_id
    barcode_flowable = code128.Code128(
        tracking_val,
        barHeight=36,
        barWidth=1.2,
        humanReadable=True,
    )

    barcode_box_data = [
        [
            Paragraph("<b>Courier Partner:</b> Steadfast Courier<br/>"
                      f"<b>Consignment ID:</b> {order.consignment_id or 'Pending Dispatch'}<br/>"
                      f"<b>Tracking Code:</b> {order.tracking_code or 'SF-PENDING'}", style_value),
            barcode_flowable,
        ]
    ]
    barcode_table = Table(barcode_box_data, colWidths=[280, 242])
    barcode_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 1, SLATE_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(barcode_table)
    story.append(Spacer(1, 14))

    # ---------------------------------------------------------------------
    # Recipient & Delivery Information Box
    # ---------------------------------------------------------------------
    zone_str = get_zone_display_name(order.delivery_zone)
    info_data = [
        [
            Paragraph("<b>RECIPIENT / DELIVERY DETAILS</b>", style_label),
            Paragraph("<b>ORDER & PAYMENT DETAILS</b>", style_label),
        ],
        [
            Paragraph(f"<b>Name:</b> {order.recipient_name}<br/>"
                      f"<b>Phone:</b> {order.recipient_phone}<br/>"
                      f"<b>Address:</b> {order.recipient_address}<br/>"
                      f"<b>Delivery Zone:</b> {zone_str}", style_value),
            Paragraph(f"<b>Status:</b> {order.status.value}<br/>"
                      f"<b>Payment Method:</b> Cash on Delivery (COD)<br/>"
                      f"<b>Delivery Charge:</b> ৳{order.delivery_fee:.2f}<br/>"
                      f"<b>Total Items:</b> {len(order.items)}", style_value),
        ],
    ]
    info_table = Table(info_data, colWidths=[261, 261])
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.75, SLATE_BORDER),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, SLATE_BORDER),
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BG),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 14))

    # ---------------------------------------------------------------------
    # Itemized Breakdown Table
    # ---------------------------------------------------------------------
    items_header = [
        Paragraph("SL", style_table_header),
        Paragraph("SKU", style_table_header),
        Paragraph("Item Description", style_table_header),
        Paragraph("Qty", style_table_header),
        Paragraph("Unit Price", style_table_header),
        Paragraph("Total (BDT)", style_table_header),
    ]

    items_rows = [items_header]

    if order.items:
        for idx, item in enumerate(order.items, start=1):
            items_rows.append([
                Paragraph(str(idx), style_table_cell),
                Paragraph(item.sku, style_table_cell),
                Paragraph(item.name, style_table_cell),
                Paragraph(str(item.qty), style_table_cell),
                Paragraph(f"৳{item.price:.2f}", style_table_cell),
                Paragraph(f"৳{item.total:.2f}", style_table_cell_bold),
            ])
    else:
        # Fallback single generic row if no discrete items listed
        items_rows.append([
            Paragraph("1", style_table_cell),
            Paragraph("GEN-01", style_table_cell),
            Paragraph("Parcel Item(s)", style_table_cell),
            Paragraph("1", style_table_cell),
            Paragraph(f"৳{(order.cod_amount - order.delivery_fee):.2f}", style_table_cell),
            Paragraph(f"৳{(order.cod_amount - order.delivery_fee):.2f}", style_table_cell_bold),
        ])

    items_table = Table(items_rows, colWidths=[28, 70, 204, 40, 80, 100])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), OBSIDIAN_DARK),
        ("BOX", (0, 0), (-1, -1), 0.75, SLATE_BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, SLATE_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # ---------------------------------------------------------------------
    # Totals Summary Block
    # ---------------------------------------------------------------------
    items_subtotal = order.calculate_items_subtotal() if order.items else (order.cod_amount - order.delivery_fee)

    totals_data = [
        [
            Paragraph("<b>Items Subtotal:</b>", style_label),
            Paragraph(f"৳{items_subtotal:.2f}", style_meta_right),
        ],
        [
            Paragraph(f"<b>Delivery Fee ({order.delivery_zone.value}):</b>", style_label),
            Paragraph(f"৳{order.delivery_fee:.2f}", style_meta_right),
        ],
        [
            Paragraph("<b>TOTAL COD AMOUNT:</b>", style_label),
            Paragraph(f"৳{order.cod_amount:.2f}", style_total_highlight),
        ],
    ]
    totals_table = Table(totals_data, colWidths=[150, 120])
    totals_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 1), (-1, 1), 1, SLATE_BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    # Wrap totals aligned to the right
    wrapper_data = [
        [
            Paragraph("<b>Instructions to Delivery Courier:</b><br/>"
                      "1. Verify customer mobile number before delivery.<br/>"
                      "2. Collect exact COD amount in Cash.<br/>"
                      "3. Do not open package before payment.", style_table_cell),
            totals_table,
        ]
    ]
    wrapper_table = Table(wrapper_data, colWidths=[252, 270])
    wrapper_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(KeepTogether(wrapper_table))

    story.append(Spacer(1, 24))
    story.append(Paragraph("Thank you for your order! Powered by Clutch (ক্ল্যাচ) Automated F-Commerce Engine.", style_footer))

    # 3. Build Document
    doc.build(story)

    # 4. Attach generated path to order model instance
    order.invoice_url = pdf_filepath

    return pdf_filepath
