"""
Supplier Report Portal — app HOÀN TOÀN TÁCH BIỆT với review_app.py (nội bộ Nilorn).

Đây là app dành cho NHÀ CUNG CẤP truy cập qua link bảo mật (kèm token) được CS gửi qua email —
KHÔNG chứa bất kỳ tab/dữ liệu nội bộ nào (không có danh sách sản phẩm/nhà cung cấp khác/thương
hiệu/root cause/CAPA có sẵn) — cố tình tách file riêng để đảm bảo an toàn dữ liệu tuyệt đối,
không phụ thuộc vào việc code nội bộ có lỗi ẩn/sót gì hay không.

Chạy thử: streamlit run supplier_portal.py
Deploy: deploy app này lên 1 URL RIÊNG (khác với review_app.py), rồi cập nhật SUPPLIER_PORTAL_BASE_URL
trong review_app.py trỏ đúng vào URL đó.
"""

import streamlit as st
import psycopg2
import anthropic
import json
import re
import base64
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

# ============================================================
# THÔNG TIN KẾT NỐI — đọc từ Streamlit Secrets, KHÔNG viết thẳng key/mật khẩu vào code nữa.
# Lý do: file này sẽ được đẩy lên GitHub để deploy lên Streamlit Community Cloud — nếu để lộ
# key/mật khẩu thật trong code, bất kỳ ai xem được repo cũng lấy được, kể cả repo Private.
# Cách thiết lập:
#   - Chạy local: tạo file .streamlit/secrets.toml (KHÔNG commit lên git — thêm vào .gitignore)
#     cùng thư mục với file này, nội dung xem hướng dẫn kèm theo.
#   - Deploy Streamlit Cloud: vào App settings → Secrets, dán đúng nội dung tương tự vào đó.
# ============================================================
DB_HOST = st.secrets.get("DB_HOST", "aws-0-ap-northeast-2.pooler.supabase.com")
DB_PORT = st.secrets.get("DB_PORT", 5432)
DB_NAME = st.secrets.get("DB_NAME", "postgres")
DB_USER = st.secrets.get("DB_USER", "postgres.sdlkfcwjfvtvpjwcmdxr")
DB_PASSWORD = st.secrets.get("DB_PASSWORD", "")
ANTHROPIC_API_KEY = st.secrets.get("ANTHROPIC_API_KEY", "")

# Thông báo qua email — cùng tài khoản Gmail dùng trong review_app.py.
GMAIL_NOTIFY_ADDRESS = st.secrets.get("GMAIL_NOTIFY_ADDRESS", "")
GMAIL_NOTIFY_APP_PASSWORD = st.secrets.get("GMAIL_NOTIFY_APP_PASSWORD", "")

# Link trực tiếp tới review_app.py (chạy nội bộ tại công ty) — chèn vào email báo CS để bấm mở app
# ngay, dù chưa auto-login được (không có Graph API). TODO: điền đúng địa chỉ LAN thật của máy chủ
# nội bộ sau khi đã host review_app.py cố định (dạng http://192.168.x.x:8501) — để trống thì email
# vẫn gửi bình thường, chỉ là không có link kèm theo.
REVIEW_APP_URL = st.secrets.get("REVIEW_APP_URL", "http://172.16.60.151:8501")

if not DB_PASSWORD or not ANTHROPIC_API_KEY:
    st.error(
        "⚠️ Thiếu cấu hình Secrets (DB_PASSWORD / ANTHROPIC_API_KEY) — app này đọc thông tin nhạy "
        "cảm từ Streamlit Secrets, không còn viết thẳng trong code. Xem hướng dẫn thiết lập "
        "secrets.toml (local) hoặc App settings → Secrets (Streamlit Cloud)."
    )
    st.stop()


def send_notification_email(to_addrs, subject, body):
    """Gửi email thông báo qua Gmail SMTP — xem giải thích đầy đủ ở review_app.py. Trả về
    (True, None) nếu thành công, (False, lý_do_lỗi) nếu thất bại — KHÔNG raise exception (không
    làm gián đoạn việc nhà cung cấp nộp báo cáo)."""
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs = [a for a in to_addrs if a]
    if not to_addrs:
        return False, "Không có địa chỉ email nhận / No recipient email address"
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


def get_ai_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def extract_text(response):
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


# ============================================================
# TOKEN — xác thực link, lấy thông tin complaint (CHỈ các trường an toàn để hiện cho NCC)
# ============================================================
def fetch_token_info(conn, token):
    with conn.cursor() as cur:
        cur.execute("""
            select t.complaint_id, t.supplier_name, t.expires_at, t.submitted_at,
                   c.notes, c.date_opened, c.so_po, c.quantity_affected, p.name
            from supplier_report_token t
            join complaint c on c.complaint_id = t.complaint_id
            left join product p on c.product_id = p.product_id
            where t.token = %s;
        """, (token,))
        return cur.fetchone()


def load_taxonomy_list(conn, kind):
    table, code_col, name_col = {
        "Root Cause": ("root_cause_taxonomy", "root_cause_code", "root_cause"),
        "CAPA": ("capa_taxonomy", "capa_code", "capa_action"),
    }[kind]
    with conn.cursor() as cur:
        cur.execute(f"select {code_col}, {name_col}, description from {table} order by {code_col};")
        rows = cur.fetchall()
    return [{"code": r[0], "name": r[1], "description": r[2]} for r in rows]


def classify_description(client, description, taxonomy_list, kind):
    """Đối chiếu mô tả NHÀ CUNG CẤP tự gõ với danh mục THẬT — CHỈ chạy phía server (không hiện
    danh mục ra cho nhà cung cấp thấy), giống hệt logic trong review_app.py."""
    taxonomy_text = "\n".join(f"- {t['code']}: {t['name']} — {t['description']}" for t in taxonomy_list)
    prompt = f"""Bạn là trợ lý QA cho nhà máy sản xuất nhãn/bao bì apparel branding.
Dưới đây là danh mục {kind} hiện có:

{taxonomy_text}

Mô tả do nhà cung cấp tự viết (có thể tiếng Anh hoặc tiếng Việt):
"{description}"

Nhiệm vụ: xác định mô tả này có khớp với 1 mã {kind} nào có sẵn ở trên không.
Chỉ trả lời bằng JSON đúng định dạng sau, không thêm chữ nào khác:

{{
  "matched_code": "<mã nếu khớp rõ ràng, hoặc null nếu không>",
  "confidence": "<High|Medium|Low>",
  "suggested_name": "<tên ngắn gọn nếu là mã mới, tiếng Anh>",
  "closest_existing_code": "<mã gần giống nhất dù không khớp hoàn toàn, hoặc null>",
  "reasoning": "<1-2 câu giải thích, tiếng Việt>"
}}
"""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = extract_text(resp).strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def submit_supplier_report(conn, ai_client, complaint_id, root_cause_texts, capa_items, signature, signature_image_b64=None):
    """Xử lý báo cáo nhà cung cấp gửi lên (hỗ trợ NHIỀU root cause / CAPA, tối đa 4 mỗi loại — đồng
    bộ với phần còn lại của hệ thống) — đối chiếu với danh mục thật, gán trực tiếp nếu khớp rõ ràng,
    hoặc gửi vào hàng đợi Duyệt Taxonomy nếu là loại mới/chưa chắc chắn."""
    summary = []

    rc_list = None
    for idx, rc_text in enumerate(root_cause_texts, start=1):
        if not rc_text or not rc_text.strip():
            continue
        if rc_list is None:
            rc_list = load_taxonomy_list(conn, "Root Cause")
        result = classify_description(ai_client, rc_text.strip(), rc_list, "Root Cause")
        conn = ensure_connection()
        if result.get("matched_code") and result.get("confidence") == "High":
            with conn.cursor() as cur:
                cur.execute(
                    "insert into complaint_root_cause (complaint_id, root_cause_code, notes) values (%s, %s, %s);",
                    (complaint_id, result["matched_code"], rc_text.strip()),
                )
                cur.execute(
                    "update complaint set root_cause_code = coalesce(root_cause_code, %s) where complaint_id = %s;",
                    (result["matched_code"], complaint_id),
                )
            conn.commit()
            summary.append(f"Root cause #{idx}: khớp mã có sẵn {result['matched_code']} — \"{rc_text.strip()}\"")
        else:
            closest = result.get("closest_existing_code") or result.get("matched_code")
            note = f"Nhà cung cấp tự mô tả (#{idx}): {rc_text.strip()}. AI kiểm tra chéo: {result.get('reasoning', '')}"
            with conn.cursor() as cur:
                cur.execute(
                    """insert into taxonomy_suggestion (complaint_id, suggestion_type, ai_suggested_name, ai_reasoning, closest_existing_code)
                       values (%s, 'Root Cause', %s, %s, %s);""",
                    (complaint_id, result.get("suggested_name") or rc_text.strip()[:100], note, closest),
                )
            conn.commit()
            summary.append(f"Root cause #{idx}: đã gửi vào hàng đợi chờ duyệt (có thể là loại mới) — \"{rc_text.strip()}\"")

    capa_list = None
    for idx, (capa_text, responsible) in enumerate(capa_items, start=1):
        if not capa_text or not capa_text.strip():
            continue
        if capa_list is None:
            capa_list = load_taxonomy_list(conn, "CAPA")
        result = classify_description(ai_client, capa_text.strip(), capa_list, "CAPA")
        conn = ensure_connection()
        if result.get("matched_code") and result.get("confidence") == "High":
            with conn.cursor() as cur:
                cur.execute(
                    """insert into capa_action (complaint_id, capa_code, responsible_party, verification_result)
                       values (%s, %s, %s, 'Pending');""",
                    (complaint_id, result["matched_code"], responsible or None),
                )
            conn.commit()
            summary.append(
                f"CAPA #{idx}: khớp mã có sẵn {result['matched_code']} — \"{capa_text.strip()}\""
                + (f" (phụ trách: {responsible})" if responsible else "")
            )
        else:
            closest = result.get("closest_existing_code") or result.get("matched_code")
            note = (
                f"Nhà cung cấp tự đề xuất (#{idx}): {capa_text.strip()}. Người phụ trách dự kiến: {responsible or '(chưa rõ)'}. "
                f"AI kiểm tra chéo: {result.get('reasoning', '')}"
            )
            with conn.cursor() as cur:
                cur.execute(
                    """insert into taxonomy_suggestion (complaint_id, suggestion_type, ai_suggested_name, ai_reasoning, closest_existing_code, responsible_party)
                       values (%s, 'CAPA', %s, %s, %s, %s);""",
                    (complaint_id, result.get("suggested_name") or capa_text.strip()[:100], note, closest, responsible or None),
                )
            conn.commit()
            summary.append(
                f"CAPA #{idx}: đã gửi vào hàng đợi chờ duyệt (có thể là loại mới) — \"{capa_text.strip()}\""
                + (f" (phụ trách: {responsible})" if responsible else "")
            )

    with conn.cursor() as cur:
        cur.execute(
            "update supplier_report_token set submitted_at = %s, supplier_signature = %s, "
            "supplier_signature_image = %s where complaint_id = %s and submitted_at is null;",
            (datetime.now(), signature, signature_image_b64, complaint_id),
        )
    conn.commit()

    # Báo cho đúng CS staff đã yêu cầu nhà cung cấp này — không phụ thuộc Microsoft 365 công ty.
    # Ghi lại kết quả vào bảng notify_debug_log (thay vì chỉ print ra console) để xem trực tiếp qua
    # Supabase Table Editor — đáng tin cậy hơn xem log trực tiếp trên Streamlit Cloud.
    def _log_notify_attempt(cs_email_val, mail_ok_val, mail_err_val):
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into notify_debug_log (complaint_id, cs_email, mail_ok, mail_err) "
                    "values (%s, %s, %s, %s);",
                    (complaint_id, cs_email_val, mail_ok_val, mail_err_val),
                )
            conn.commit()
        except Exception:
            pass

    with conn.cursor() as cur:
        cur.execute("""
            select cs.email, t.supplier_name, c.so_po, t.requested_by_staff_id
            from supplier_report_token t
            left join cs_staff cs on t.requested_by_staff_id = cs.staff_id
            left join complaint c on t.complaint_id = c.complaint_id
            where t.complaint_id = %s
            order by t.created_at desc limit 1;
        """, (complaint_id,))
        notify_row = cur.fetchone()
    if notify_row and notify_row[0]:
        cs_email, supplier_name_notify, so_po_notify, _requested_by_id = notify_row
        summary_lines_text = "\n".join(f"- {line}" for line in summary)
        has_pending = any("hàng đợi chờ duyệt" in line for line in summary)
        if has_pending:
            next_step = (
                "⏳ Có mục đang chờ duyệt — vào tab 'Duyệt Taxonomy' để duyệt trước, sau đó mới sang "
                "'Truy xuất dữ liệu' để soạn email trả lời khách hàng đầy đủ."
            )
        else:
            next_step = (
                "✅ Mọi root cause/CAPA đã khớp mã có sẵn — có thể vào ngay tab 'Truy xuất dữ liệu' "
                "để soạn email trả lời khách hàng."
            )
        app_link_line = f"Mở app: {REVIEW_APP_URL}\n\n" if REVIEW_APP_URL else ""
        mail_ok, mail_err = send_notification_email(
            cs_email,
            f"[Nilorn Internal AI] {supplier_name_notify} vừa nộp báo cáo",
            f"Nhà cung cấp {supplier_name_notify} vừa nộp báo cáo điều tra cho complaint "
            f"{so_po_notify or complaint_id}.\n\n"
            f"Kết quả xử lý tự động:\n{summary_lines_text}\n\n"
            f"{next_step}\n\n"
            f"{app_link_line}"
            f"Vào app 'Nilorn Internal AI' → tab 'Truy xuất dữ liệu' → chọn đúng complaint để xem chi tiết.",
        )
        _log_notify_attempt(cs_email, mail_ok, mail_err)
    else:
        _log_notify_attempt(None, False, f"SKIPPED — no cs.email found (requested_by_staff_id={notify_row[3] if notify_row else None})")

    return summary


# ============================================================
# GIAO DIỆN — chỉ 1 trang duy nhất, không có tab nào khác
# ============================================================
st.set_page_config(page_title="Supplier Quality Report", layout="centered")
st.title("📋 Quality Issue Report Form")

query_params = st.query_params
token = query_params.get("token", "")

if not token:
    st.error("Missing report link token. Please use the exact link provided in the request email.")
    st.stop()

conn = ensure_connection()
info = fetch_token_info(conn, token)

if not info:
    st.error("This report link is invalid. Please contact us if you believe this is an error.")
    st.stop()

(complaint_id, supplier_name, expires_at, submitted_at,
 notes, date_opened, so_po, qty_affected, product_name) = info

if submitted_at:
    st.success("✅ This report has already been submitted. Thank you — no further action is needed.")
    st.caption(f"Submitted on {submitted_at.strftime('%Y-%m-%d %H:%M')}.")
    st.stop()

if expires_at and datetime.now(expires_at.tzinfo) > expires_at:
    st.error("This report link has expired. Please contact us for a new link.")
    st.stop()

st.write(f"**Supplier:** {supplier_name}")
st.markdown("---")
st.subheader("Issue summary")
st.write(f"**Product:** {product_name or '(not specified)'}")
st.write(f"**SO/PO:** {so_po or '(not specified)'}")
st.write(f"**Date reported:** {date_opened.strftime('%Y-%m-%d') if date_opened else '(not specified)'}")
st.write(f"**Affected quantity:** {qty_affected if qty_affected is not None else '(not specified)'}")
st.write(f"**Description:** {notes or '(not specified)'}")
st.markdown("---")

st.subheader("Please complete the following")
st.caption("Based on your own investigation — describe in your own words, no need to reference any internal codes.")

col_a, col_b = st.columns(2)
with col_a:
    num_root_causes = st.number_input(
        "How many separate root causes are you reporting?", min_value=1, max_value=4, step=1, value=1,
    )
with col_b:
    num_capas = st.number_input(
        "How many separate corrective actions (CAPA) are you reporting?", min_value=1, max_value=4, step=1, value=1,
    )

with st.form("supplier_report_form"):
    root_cause_inputs = []
    for i in range(num_root_causes):
        root_cause_inputs.append(st.text_area(
            f"Root cause #{i + 1} — what caused this issue on your end? *",
            height=100, placeholder="Describe the root cause you identified...", key=f"rc_input_{i}",
        ))

    capa_inputs = []
    for i in range(num_capas):
        st.markdown(f"**Corrective action #{i + 1}**")
        capa_text_i = st.text_area(
            "What action have you taken or will take? *",
            height=100, placeholder="Describe the corrective action...", key=f"capa_text_{i}",
        )
        capa_resp_i = st.text_input("Person responsible for this action *", key=f"capa_resp_{i}")
        capa_inputs.append((capa_text_i, capa_resp_i))

    signature_input = st.text_input("Your name (as confirmation / signature) *")
    signature_image_input = st.file_uploader(
        "Signature image (optional — e.g. a photo or scan of your signature)",
        type=["png", "jpg", "jpeg"], key="signature_image_input",
    )
    st.caption("* Required — all fields marked with an asterisk must be filled in before you can submit.")

    submitted = st.form_submit_button("✅ Submit Report")

    if submitted:
        # Bắt buộc điền ĐỦ mọi trường (không chỉ tối thiểu 1) trước khi cho phép nộp — liệt kê
        # cụ thể từng trường còn thiếu để nhà cung cấp biết chính xác cần bổ sung chỗ nào.
        missing = []
        if not signature_input.strip():
            missing.append("your name (confirmation / signature)")
        for i, rc_text in enumerate(root_cause_inputs, start=1):
            if not rc_text.strip():
                missing.append(f"Root cause #{i}")
        for i, (capa_text_i, capa_resp_i) in enumerate(capa_inputs, start=1):
            if not capa_text_i.strip():
                missing.append(f"Corrective action #{i} — description")
            if not capa_resp_i.strip():
                missing.append(f"Corrective action #{i} — person responsible")

        if missing:
            st.warning("Please complete the following before submitting: " + "; ".join(missing) + ".")
        else:
            signature_image_b64 = None
            if signature_image_input is not None:
                signature_image_b64 = base64.b64encode(signature_image_input.getvalue()).decode("utf-8")
            with st.spinner("Submitting..."):
                ai_client = get_ai_client()
                summary = submit_supplier_report(
                    conn, ai_client, complaint_id,
                    root_cause_inputs, capa_inputs, signature_input.strip(),
                    signature_image_b64=signature_image_b64,
                )
            st.success("✅ Thank you — your report has been submitted successfully.")
            for line in summary:
                st.write(f"- {line}")
            st.stop()
