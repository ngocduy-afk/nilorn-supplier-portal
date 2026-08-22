"""
Supplier Report Portal — LUỒNG MỚI (link chung, không gắn theo từng complaint).

Nhà cung cấp tự khai báo TOÀN BỘ thông tin (không cần CS tạo trước link riêng cho từng đơn) —
hệ thống tự động đối chiếu ra đúng Vendor No. dựa theo tên NCC tự gõ, không cần CS duyệt gì cả.
Deploy: Streamlit Community Cloud (public URL cố định, dùng mãi — không đổi mỗi lần).
"""

import streamlit as st
import psycopg2
import base64
import re
import uuid
import difflib
import smtplib
import requests
from email.mime.text import MIMEText
from datetime import datetime

# ============================================================
# THÔNG TIN KẾT NỐI — đọc từ Streamlit Secrets, không viết thẳng vào code.
# ============================================================
DB_HOST = st.secrets.get("DB_HOST", "aws-0-ap-northeast-2.pooler.supabase.com")
DB_PORT = st.secrets.get("DB_PORT", 5432)
DB_NAME = st.secrets.get("DB_NAME", "postgres")
DB_USER = st.secrets.get("DB_USER", "postgres.sdlkfcwjfvtvpjwcmdxr")
DB_PASSWORD = st.secrets.get("DB_PASSWORD", "")

GMAIL_NOTIFY_ADDRESS = st.secrets.get("GMAIL_NOTIFY_ADDRESS", "")
GMAIL_NOTIFY_APP_PASSWORD = st.secrets.get("GMAIL_NOTIFY_APP_PASSWORD", "")
REVIEW_APP_URL = st.secrets.get("REVIEW_APP_URL", "http://172.16.60.151:8501")

# Danh sách email nhận thông báo "có báo cáo mới từ NCC" — điền toàn bộ CS cần biết.
TEAM_EMAILS = [e.strip() for e in st.secrets.get("TEAM_EMAILS", "").split(",") if e.strip()]

# Supabase Storage — dùng để lưu ảnh/video (KHÔNG nhét base64 vào database nữa, tránh phình dung
# lượng — đặc biệt quan trọng với video vì file lớn hơn ảnh rất nhiều).
SUPABASE_PROJECT_REF = st.secrets.get("SUPABASE_PROJECT_REF", "sdlkfcwjfvtvpjwcmdxr")
SUPABASE_URL = f"https://{SUPABASE_PROJECT_REF}.supabase.co"
SUPABASE_SERVICE_KEY = st.secrets.get("SUPABASE_SERVICE_KEY", "")
STORAGE_BUCKET = "defect-media"

MAX_IMAGE_MB = 8
MAX_VIDEO_MB = 25

if not DB_PASSWORD:
    st.error(
        "⚠️ Thiếu cấu hình Secrets (DB_PASSWORD) — vào App settings → Secrets để điền. "
        "/ Missing Secrets configuration (DB_PASSWORD) — go to App settings → Secrets."
    )
    st.stop()


def get_connection():
    if "db_conn" not in st.session_state or st.session_state.db_conn.closed:
        st.session_state.db_conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
        )
    return st.session_state.db_conn


def ensure_connection():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
    except Exception:
        try:
            st.session_state.db_conn.close()
        except Exception:
            pass
        st.session_state.db_conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
        )
    return st.session_state.db_conn


# ============================================================
# SUPABASE STORAGE — upload ảnh/video, trả về URL công khai.
# ============================================================
def upload_to_storage(file_bytes, filename, content_type):
    """Upload 1 file lên Supabase Storage bucket, trả về URL công khai để lưu vào database.
    Raise Exception nếu upload thất bại — nơi gọi cần tự bắt lỗi và báo cho người dùng."""
    if not SUPABASE_SERVICE_KEY:
        raise Exception("Chưa cấu hình SUPABASE_SERVICE_KEY trong Secrets.")
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    path = f"{datetime.now().strftime('%Y%m%d')}/{uuid.uuid4().hex}_{safe_name}"
    url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
        "Content-Type": content_type or "application/octet-stream",
    }
    resp = requests.post(url, headers=headers, data=file_bytes, timeout=60)
    if resp.status_code not in (200, 201):
        raise Exception(f"Upload thất bại ({resp.status_code}): {resp.text[:200]}")
    return f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{path}"


# ============================================================
# GỬI EMAIL THÔNG BÁO — Gmail SMTP riêng, không qua Microsoft 365 công ty.
# ============================================================
def send_notification_email(to_addrs, subject, body):
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs = [a for a in to_addrs if a]
    if not to_addrs:
        return False, "Không có địa chỉ email nhận / No recipient"
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_NOTIFY_ADDRESS
        msg["To"] = ", ".join(to_addrs)
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.starttls()
            server.login(GMAIL_NOTIFY_ADDRESS, GMAIL_NOTIFY_APP_PASSWORD)
            server.sendmail(GMAIL_NOTIFY_ADDRESS, to_addrs, msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)


def _log_notify_attempt(conn, submission_id, cs_emails, mail_ok, mail_err):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "insert into notify_debug_log (complaint_id, cs_email, mail_ok, mail_err) "
                "values (%s, %s, %s, %s);",
                (submission_id, ", ".join(cs_emails) if cs_emails else None, mail_ok, mail_err),
            )
        conn.commit()
    except Exception:
        pass


# ============================================================
# ĐỐI CHIẾU VENDOR — NCC tự gõ tên công ty, hệ thống tự tra ra đúng Vendor No.
# ============================================================
def normalize_text(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def match_vendor(conn, typed_name):
    """Trả về (vendor_code, vendor_name, confidence) — confidence: 'exact' | 'fuzzy' | 'unmatched'."""
    typed_norm = normalize_text(typed_name)
    if not typed_norm:
        return None, None, "unmatched"
    with conn.cursor() as cur:
        cur.execute("select vendor_code, vendor_name from vendor_lookup;")
        rows = cur.fetchall()
    for code, name in rows:
        if normalize_text(name) == typed_norm:
            return code, name, "exact"
    best_code, best_name, best_ratio = None, None, 0.0
    for code, name in rows:
        ratio = difflib.SequenceMatcher(None, normalize_text(name), typed_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_code, best_name = ratio, code, name
    if best_ratio >= 0.55:
        return best_code, best_name, "fuzzy"
    return None, None, "unmatched"


# ============================================================
# NỐI DỮ LIỆU SANG BẢNG CŨ (complaint / taxonomy_suggestion) — để tab "Truy xuất dữ liệu" và
# "Hỏi AI" (đọc từ bảng complaint) cũng thấy được complaint từ luồng mới này. Root Cause/CAPA nhà
# cung cấp tự gõ được đưa vào hàng đợi "Pending" — tận dụng đúng cơ chế Duyệt Taxonomy + email báo
# reviewer đã có sẵn, không xây lại từ đầu. Khi reviewer duyệt xong, cơ chế cũ tự cập nhật thẳng
# vào bảng complaint — không cần thêm code đồng bộ nào nữa.
# ============================================================
def get_or_create_supplier(conn, name):
    if not name:
        return None
    with conn.cursor() as cur:
        cur.execute("select supplier_id from supplier where name = %s limit 1;", (name,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("insert into supplier (name) values (%s) returning supplier_id;", (name,))
        new_id = cur.fetchone()[0]
    conn.commit()
    return new_id


def bridge_to_legacy_tables(conn, submission_id, record_date, supplier_name_raw, vendor_name_matched,
                             sales_order_no, purchase_order_no, order_qty, defect_qty,
                             description, root_cause_text, capa_text):
    supplier_id = get_or_create_supplier(conn, vendor_name_matched or supplier_name_raw)
    with conn.cursor() as cur:
        cur.execute(
            """insert into complaint
                   (date_opened, source, supplier_id, so_po, quantity_inspected, quantity_affected,
                    notes, status)
               values (%s, %s, %s, %s, %s, %s, %s, 'Open')
               returning complaint_id;""",
            (record_date, "Nhà cung cấp tự khai báo (link chung)", supplier_id,
             f"{sales_order_no} / {purchase_order_no}", order_qty, defect_qty, description),
        )
        complaint_id = cur.fetchone()[0]

        cur.execute(
            """insert into taxonomy_suggestion
                   (complaint_id, suggestion_type, ai_suggested_name, ai_reasoning)
               values (%s, 'Root Cause', %s, %s);""",
            (complaint_id, root_cause_text[:200], "Do nhà cung cấp tự gõ khi nộp báo cáo qua link chung."),
        )
        cur.execute(
            """insert into taxonomy_suggestion
                   (complaint_id, suggestion_type, ai_suggested_name, ai_reasoning)
               values (%s, 'CAPA', %s, %s);""",
            (complaint_id, capa_text[:200], "Do nhà cung cấp tự gõ khi nộp báo cáo qua link chung."),
        )
    conn.commit()
    return complaint_id


# ============================================================
# GIAO DIỆN
# ============================================================
st.set_page_config(page_title="Nilorn Supplier Quality Report", layout="centered")
st.markdown(
    """<style>
.stApp { background: #f4f5f8; }
.fc-required { color: #a32d2d; font-weight: 600; }
</style>""",
    unsafe_allow_html=True,
)

st.title("📋 Nilorn — Supplier Quality Issue Report")
st.caption(
    "Please complete all fields below to report a quality issue. All fields marked with * are required."
)

with st.form("supplier_report_form", clear_on_submit=False):
    st.subheader("1. Your Company & Order Information")
    supplier_name_raw = st.text_input("Supplier / Vendor Company Name *", placeholder="e.g. Nilorn Vietnam Company Limited")
    record_date_in = st.date_input("Record Date *", value=datetime.now().date())
    col1, col2 = st.columns(2)
    with col1:
        sales_order_no = st.text_input("Sales Order No. *")
        item_no = st.text_input("Item No. *")
        order_qty = st.number_input("Order Qty *", min_value=0, step=1)
    with col2:
        purchase_order_no = st.text_input("Purchase Order No. *")
        defect_qty = st.number_input("Defect Qty *", min_value=0, step=1)

    st.markdown("---")
    st.subheader("2. Issue Details")
    description = st.text_area("Issue Description *", height=90, placeholder="What is the defect?")
    root_cause = st.text_area("Root Cause *", height=90, placeholder="What caused this issue on your end?")
    capa = st.text_area("Corrective Action (CAPA) *", height=90, placeholder="What action have you taken or will take?")
    capa_status = st.selectbox("CAPA Status *", ["In Progress", "Completed", "Pending Verification", "Closed"])

    st.markdown("---")
    st.subheader("3. Photos & Video")
    st.caption(
        "📷 **Photos are strongly preferred over video** — they upload faster and are easier for us to "
        "review. Please only attach a video if photos alone cannot show the issue clearly."
    )
    defect_images = st.file_uploader(
        f"Defect photo(s) * — at least 1 required, up to 3 (max {MAX_IMAGE_MB}MB each)",
        type=["png", "jpg", "jpeg"], accept_multiple_files=True,
    )
    defect_video = st.file_uploader(
        f"Defect video (optional, max {MAX_VIDEO_MB}MB — roughly a 30-45 second clip)",
        type=["mp4", "mov", "avi", "webm"],
    )

    submitted = st.form_submit_button("✅ Submit Report")

if submitted:
    missing = []
    if not supplier_name_raw.strip():
        missing.append("Supplier / Vendor Company Name")
    if not sales_order_no.strip():
        missing.append("Sales Order No.")
    if not purchase_order_no.strip():
        missing.append("Purchase Order No.")
    if not item_no.strip():
        missing.append("Item No.")
    if not order_qty:
        missing.append("Order Qty")
    if not defect_qty:
        missing.append("Defect Qty")
    if not description.strip():
        missing.append("Issue Description")
    if not root_cause.strip():
        missing.append("Root Cause")
    if not capa.strip():
        missing.append("Corrective Action (CAPA)")
    if not defect_images:
        missing.append("At least 1 defect photo")

    oversized = []
    if defect_images:
        for f in defect_images[:3]:
            if len(f.getvalue()) > MAX_IMAGE_MB * 1024 * 1024:
                oversized.append(f.name)
    if defect_video and len(defect_video.getvalue()) > MAX_VIDEO_MB * 1024 * 1024:
        oversized.append(defect_video.name)

    if missing:
        st.warning("Please complete the following before submitting: " + "; ".join(missing) + ".")
    elif oversized:
        st.warning("These files exceed the size limit, please use a smaller file: " + ", ".join(oversized) + ".")
    else:
        with st.spinner("Submitting your report..."):
            conn = ensure_connection()
            vendor_code, vendor_name_matched, confidence = match_vendor(conn, supplier_name_raw)

            image_urls = []
            upload_error = None
            try:
                for f in defect_images[:3]:
                    url = upload_to_storage(f.getvalue(), f.name, f.type)
                    image_urls.append(url)
                video_url = None
                if defect_video:
                    video_url = upload_to_storage(defect_video.getvalue(), defect_video.name, defect_video.type)
            except Exception as e:
                upload_error = str(e)

            if upload_error:
                st.error(
                    f"⚠️ Could not upload photo/video — please try again or contact us. "
                    f"(Technical detail: {upload_error})"
                )
            else:
                import json as _json
                with conn.cursor() as cur:
                    cur.execute(
                        """insert into supplier_submissions
                               (record_date, supplier_name_raw, sales_order_no, purchase_order_no, item_no,
                                order_qty, defect_qty, description, root_cause, capa, capa_status,
                                defect_image_urls, defect_video_url,
                                vendor_code, vendor_name_matched, match_confidence)
                           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           returning submission_id;""",
                        (record_date_in, supplier_name_raw.strip(), sales_order_no.strip(), purchase_order_no.strip(),
                         item_no.strip(), int(order_qty), int(defect_qty), description.strip(),
                         root_cause.strip(), capa.strip(), capa_status,
                         _json.dumps(image_urls), video_url,
                         vendor_code, vendor_name_matched, confidence),
                    )
                    submission_id = cur.fetchone()[0]
                conn.commit()

                try:
                    bridge_to_legacy_tables(
                        conn, submission_id, record_date_in, supplier_name_raw.strip(),
                        vendor_name_matched, sales_order_no.strip(), purchase_order_no.strip(),
                        int(order_qty), int(defect_qty), description.strip(),
                        root_cause.strip(), capa.strip(),
                    )
                except Exception:
                    pass  # không để lỗi ở bước nối dữ liệu cũ làm ảnh hưởng việc nộp báo cáo chính

                match_note = (
                    f"Matched vendor: {vendor_name_matched} ({vendor_code})" if vendor_code
                    else "⚠️ Could not auto-match a vendor from the name provided — needs manual check."
                )
                body = (
                    f"A new supplier quality report was just submitted.\n\n"
                    f"Supplier (as typed): {supplier_name_raw.strip()}\n"
                    f"{match_note}\n\n"
                    f"Sales Order No.: {sales_order_no.strip()}\n"
                    f"Purchase Order No.: {purchase_order_no.strip()}\n"
                    f"Item No.: {item_no.strip()}\n"
                    f"Order Qty: {order_qty}  |  Defect Qty: {defect_qty}\n\n"
                    f"Description: {description.strip()}\n"
                    f"Root Cause: {root_cause.strip()}\n"
                    f"CAPA: {capa.strip()}\n"
                    f"CAPA Status: {capa_status}\n\n"
                    f"Photos attached: {len(image_urls)}" + (" | Video attached" if video_url else "") + "\n\n"
                    f"Open the app to review and complete Customer / Replacement Cost: {REVIEW_APP_URL}"
                )
                mail_ok, mail_err = send_notification_email(
                    TEAM_EMAILS, f"[Nilorn Internal AI] New supplier report — {supplier_name_raw.strip()}", body,
                )
                _log_notify_attempt(conn, submission_id, TEAM_EMAILS, mail_ok, mail_err)

                st.success("✅ Thank you — your report has been submitted successfully.")
                st.balloons()
