import hashlib
import hmac
import re
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from io import BytesIO
from time import time_ns
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore as firebase_firestore
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.field_path import FieldPath

KST = ZoneInfo("Asia/Seoul")  # the cloud server runs on UTC; welders work in Korean time
CACHE_TTL = 15  # seconds a Firestore read is reused (a write from this app clears it at once)
COOKIE_MAX_AGE = 365 * 24 * 3600  # remember 직반 / 이름 on the phone for a year

WORKERS = {
    "1직1반": ["라흐마뚤러", "에릭", "짜효노", "누로흐만", "리스키갈리", "데바", "수얏노", "에펜디", "조디", "마르또노"],
    "1직2반": ["쁘리아트모노", "위자나르코", "나우팔", "앙고로", "누르콜릭", "헤르만토", "마난", "리야디", "루이스"],
    "1직3반": ["바유함자", "아프릴라", "데디", "사뿌트라", "무지오노", "수다르모노", "유누스", "푸풍", "아흐마드샤피", "안자스"],
    "1직4반": ["라마", "샤리푸딘", "시딕", "알피안디", "카릴", "파이잘", "푸트라", "프라무디아", "헨드리"],
}

# Worker IDs are their names; passwords are the digits in their 직반,
# e.g. 에릭 in 1직1반 signs in with ID 에릭 and password 11.
WORKER_ACCOUNTS: dict[str, dict[str, str]] = {}
for _team, _names in WORKERS.items():
    _team_code = "".join(char for char in _team if char.isdigit())
    for _worker_name in _names:
        if _worker_name in WORKER_ACCOUNTS or _worker_name == "admin":
            raise ValueError(f"Duplicate or reserved login ID: {_worker_name}")
        WORKER_ACCOUNTS[_worker_name] = {"team": _team, "password": _team_code}

SPECIAL_ACCOUNTS = {
    "김대성": {"team": "1직1반", "password": "1111"},
    "김태언": {"team": "1직2반", "password": "1212"},
    "남호걸": {"team": "1직3반", "password": "1313"},
    "유현수": {"team": "1직4반", "password": "1414"},
}

ACTIVE, PAUSED, DONE, CANCELED = "진행중", "일시정지", "완료", "취소"
INPUT_MODE = "직접입력"  
JOINT_COLS = ("프로젝트번호", "SPOOL_NO", "TAG_NO", "JOINT_NO")
LOG_COLUMNS = ["id", "직반", "작업자", "프로젝트번호", "입력방식", "SPOOL_NO", "TAG_NO", "JOINT_NO", "상태", "진행률",
               "용접시작일자", "용접시작시간", "일시정지일자", "일시정지시간", "용접완료일자", "용접완료시간"]

st.set_page_config(page_title="용접 실적 입력", page_icon="🔥", layout="centered")

for _key in list(st.session_state.keys()):
    if _key.startswith(("w_", "s_", "c_")):
        st.session_state[_key] = st.session_state[_key]

if "cookie_checked" not in st.session_state:
    st.session_state.cookie_checked = True
    _team = unquote(st.context.cookies.get("welder_team", ""))
    _name = unquote(st.context.cookies.get("welder_name", ""))
    if _team in WORKERS:
        st.session_state.w_team = _team
        if _name in WORKERS[_team]:
            st.session_state.w_name = _name
            st.session_state.cookie_saved = (_team, _name)

# ---------------------------------------------------------------- data ----
def fmt(ts: datetime) -> tuple[str, str]:
    return ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M:%S")

# -------------------------------------------------------------- login ----
def configured_admin_password() -> str:
    """Read the admin password from Streamlit secrets, never from this source file."""
    auth_secrets = st.secrets.get("auth", {})
    return str(auth_secrets.get("admin_password", ""))


def authenticate(login_id: str, password: str) -> dict | None:
    if login_id == "admin":
        expected = configured_admin_password()
        if expected and hmac.compare_digest(password, expected):
            return {"role": "admin", "name": "admin"}
        return None

    special_account = SPECIAL_ACCOUNTS.get(login_id)
    if special_account and hmac.compare_digest(password, special_account["password"]):
        return {"role": "team_admin", "name": login_id, "team": special_account["team"]}

    account = WORKER_ACCOUNTS.get(login_id)
    if account and hmac.compare_digest(password, account["password"]):
        return {"role": "worker", "name": login_id, "team": account["team"]}
    return None


def login_page():
    st.title("용접 실적 입력 로그인")
    if not configured_admin_password():
        st.warning("관리자 로그인을 사용하려면 Streamlit Secrets에 [auth] admin_password를 설정하세요.")

    with st.form("login_form", clear_on_submit=True):
        login_id = st.text_input("ID", key="login_id")
        password = st.text_input("비밀번호", type="password", key="login_password")
        submitted = st.form_submit_button("로그인", type="primary", width="stretch")

    if submitted:
        user = authenticate(login_id.strip(), password)
        if user:
            st.session_state.current_user = user
            st.rerun()
        st.error("ID 또는 비밀번호가 올바르지 않습니다.")


def current_user() -> dict:
    return st.session_state.current_user


def is_admin() -> bool:
    return current_user()["role"] == "admin"


def is_team_admin() -> bool:
    return current_user()["role"] == "team_admin"


def can_manage_team() -> bool:
    return is_admin() or is_team_admin()


def can_access_record(team: str, name: str) -> bool:
    user = current_user()
    if user["role"] == "admin":
        return True
    if user["role"] == "team_admin":
        return team == user["team"]
    return team == user["team"] and name == user["name"]


# ------------------------------------------------------------ database ----
class JointTaken(Exception):
    """The joint changed state while the user was looking at it."""

@st.cache_resource
def _client() -> firestore.Client:
    if not firebase_admin._apps:
        cred = credentials.Certificate(dict(st.secrets["firebase"]))
        firebase_admin.initialize_app(cred)
    return firebase_firestore.client()

def get_db() -> firestore.Client:
    try:
        return _client()
    except Exception as e:
        st.error(f"Firebase에 연결할 수 없습니다. Secrets의 [firebase] 설정을 확인하세요. ({type(e).__name__})")
        st.stop()

def now() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)

def joint_ref(rec: dict):
    key = "|".join(str(rec[c]) for c in JOINT_COLS)
    return get_db().collection("joints").document(hashlib.sha1(key.encode("utf-8")).hexdigest())

def log_ref(seg_id: int):
    return get_db().collection("welding_log").document(str(seg_id))

def clear_cache():
    for fn in (latest_by_joint, paused_joints, _active_all, load_log, load_joint_states):
        fn.clear()

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def latest_by_joint(project: str) -> dict[tuple[str, str, str], dict]:
    project_field = FieldPath("프로젝트번호").to_api_repr()
    docs = get_db().collection("joints").where(filter=FieldFilter(project_field, "==", project)).stream()
    return {(d["SPOOL_NO"], d["TAG_NO"], d["JOINT_NO"]): d for d in (x.to_dict() for x in docs)}

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def paused_joints() -> list[dict]:
    status_field = FieldPath("상태").to_api_repr()
    docs = get_db().collection("joints").where(filter=FieldFilter(status_field, "==", PAUSED)).stream()
    rows = [d.to_dict() for d in docs]
    rows.sort(key=lambda r: (r["일시정지일자"], r["일시정지시간"]), reverse=True)
    return rows

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _active_all() -> list[dict]:
    status_field = FieldPath("상태").to_api_repr()
    docs = get_db().collection("joints").where(filter=FieldFilter(status_field, "==", ACTIVE)).stream()
    rows = [d.to_dict() for d in docs]
    rows.sort(key=lambda r: (r["용접시작일자"], r["용접시작시간"]))
    return rows

def active_segments(team: str | None = None, name: str | None = None) -> list[dict]:
    rows = _active_all()
    if name:
        return [r for r in rows if r["직반"] == team and r["작업자"] == name]
    if team:
        return [r for r in rows if r["직반"] == team]
    return rows

def joint_progress(rec: dict) -> int:
    return (joint_ref(rec).get().to_dict() or {}).get("base_progress", 0)

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_log() -> pd.DataFrame:
    rows = [d.to_dict() for d in get_db().collection("welding_log").stream()]
    df = pd.DataFrame(rows).reindex(columns=LOG_COLUMNS)
    df["진행률"] = pd.to_numeric(df["진행률"])
    return df

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_joint_states() -> list[dict]:
    return [
        {"_doc_id": snapshot.id, **snapshot.to_dict()}
        for snapshot in get_db().collection("joints").stream()
    ]

@firestore.transactional
def _start_tx(tx, jref, lref, seg: dict):
    snap = jref.get(transaction=tx)
    cur = snap.to_dict() if snap.exists else None
    if cur and cur["상태"] in (ACTIVE, DONE):
        raise JointTaken
    base = cur["base_progress"] if cur and cur["상태"] == PAUSED else 0
    tx.set(lref, seg)
    tx.set(jref, {**seg, "base_progress": base})

@firestore.transactional
def _end_tx(tx, jref, lref, seg_id: int, changes: dict, base: int):
    cur = jref.get(transaction=tx).to_dict()
    if not cur or cur["상태"] != ACTIVE or cur["id"] != seg_id:
        raise JointTaken
    tx.update(lref, changes)
    tx.update(jref, {**changes, "base_progress": base})

@firestore.transactional
def _cancel_tx(tx, jref, lref, seg_id: int):
    cur = jref.get(transaction=tx).to_dict()
    if not cur or cur["상태"] != PAUSED or cur["id"] != seg_id:
        raise JointTaken
    tx.update(lref, {"상태": CANCELED})
    tx.update(jref, {"상태": CANCELED, "base_progress": 0})

@firestore.transactional
def _rename_joint_tx(tx, old_ref, new_ref, log_refs, old_key, expected_state_id, updates):
    current_snap = old_ref.get(transaction=tx)
    target_snap = new_ref.get(transaction=tx) if old_ref.id != new_ref.id else current_snap
    log_snaps = [ref.get(transaction=tx) for ref in log_refs]
    current = current_snap.to_dict() if current_snap.exists else None
    if (
        not current
        or tuple(str(current.get(c) or "") for c in JOINT_COLS) != old_key
        or current.get("id") != expected_state_id
    ):
        raise JointTaken("이 JOINT 정보가 다른 작업에서 변경되었습니다. 새로고침 후 다시 시도하세요.")
    if old_ref.id != new_ref.id and target_snap.exists:
        raise JointTaken("수정하려는 SPOOL/JOINT 번호가 이미 등록되어 있습니다.")
    for snap in log_snaps:
        row = snap.to_dict() if snap.exists else None
        if not row or tuple(str(row.get(c) or "") for c in JOINT_COLS) != old_key:
            raise JointTaken("작업 이력이 변경되었습니다. 새로고침 후 다시 시도하세요.")

    if old_ref.id == new_ref.id:
        tx.update(old_ref, updates)
    else:
        tx.set(new_ref, {**current, **updates})
        tx.delete(old_ref)
    for ref in log_refs:
        tx.update(ref, updates)

@firestore.transactional
def _delete_joint_tx(tx, state_items, log_items):
    state_snaps = [
        (ref, expected_key, expected_id, ref.get(transaction=tx))
        for ref, expected_key, expected_id in state_items
    ]
    log_snaps = [(ref, expected_key, ref.get(transaction=tx)) for ref, expected_key in log_items]
    for _, expected_key, expected_id, snap in state_snaps:
        row = snap.to_dict() if snap.exists else None
        if (
            not row
            or tuple(str(row.get(c) or "") for c in JOINT_COLS) != expected_key
            or row.get("id") != expected_id
        ):
            raise JointTaken("선택한 데이터가 변경되어 삭제하지 않았습니다. 새로고침 후 다시 선택하세요.")
    for _, expected_key, snap in log_snaps:
        row = snap.to_dict() if snap.exists else None
        if not row or tuple(str(row.get(c) or "") for c in JOINT_COLS) != expected_key:
            raise JointTaken("선택한 데이터가 변경되어 삭제하지 않았습니다. 새로고침 후 다시 선택하세요.")
    for ref, _, _ in log_snaps:
        tx.delete(ref)
    for ref, _, _, _ in state_snaps:
        tx.delete(ref)

def joint_key(rec: dict) -> tuple[str, str, str, str]:
    return tuple(str(rec.get(c) or "") for c in JOINT_COLS)

def joint_log_refs(rec: dict, refresh: bool = False) -> list:
    if refresh:
        load_log.clear()
    df = load_log()
    if df.empty:
        return []
    mask = pd.Series(True, index=df.index)
    for column in JOINT_COLS:
        mask &= df[column].fillna("").astype(str) == str(rec.get(column) or "")
    return [log_ref(int(value)) for value in df.loc[mask, "id"].tolist()]

def rename_joint(rec: dict, spool: str, joint: str):
    if not can_manage_team() or not can_access_record(rec["직반"], rec["작업자"]):
        raise ValueError("이 JOINT를 수정할 권한이 없습니다.")
    spool, joint = spool.strip().upper(), joint.strip().upper()
    if not spool or not joint:
        raise ValueError("SPOOL NO와 JOINT NO를 모두 입력하세요.")
    updates = {"SPOOL_NO": spool, "JOINT_NO": joint}
    updated = {**rec, **updates}
    old_ref = get_db().collection("joints").document(rec["_doc_id"])
    new_ref = joint_ref(updated)
    if old_ref.id == new_ref.id and joint_key(rec) == joint_key(updated):
        return
    logs = joint_log_refs(rec, refresh=True)
    if len(logs) + (1 if old_ref.id == new_ref.id else 2) > 500:
        raise ValueError("이 JOINT의 이력이 너무 많아 한 번에 수정할 수 없습니다. 관리자에게 문의하세요.")
    try:
        _rename_joint_tx(
            get_db().transaction(), old_ref, new_ref, logs, joint_key(rec), rec.get("id"), updates
        )
    except JointTaken as exc:
        raise ValueError(str(exc)) from exc
    clear_cache()

def visible_joint_states() -> list[dict]:
    rows = load_joint_states()
    user = current_user()
    if is_admin():
        return rows
    return [row for row in rows if row.get("직반") == user["team"]]

def delete_joint_records(doc_ids: list[str]):
    states = {row["_doc_id"]: row for row in visible_joint_states()}
    selected = [states[doc_id] for doc_id in doc_ids if doc_id in states]
    if len(selected) != len(set(doc_ids)):
        raise ValueError("선택한 JOINT 중 일부에 접근할 수 없습니다. 새로고침 후 다시 선택하세요.")
    state_items = []
    log_items_by_id = {}
    load_log.clear()
    for rec in selected:
        if not can_access_record(rec["직반"], rec["작업자"]):
            raise ValueError("선택한 JOINT를 삭제할 권한이 없습니다.")
        key = joint_key(rec)
        state_items.append((get_db().collection("joints").document(rec["_doc_id"]), key, rec.get("id")))
        for ref in joint_log_refs(rec):
            log_items_by_id[ref.id] = (ref, key)
    log_items = list(log_items_by_id.values())
    if len(state_items) + len(log_items) > 500:
        raise ValueError("선택한 이력이 많아 한 번에 삭제할 수 없습니다. 더 적은 수를 선택하세요.")
    try:
        _delete_joint_tx(get_db().transaction(), state_items, log_items)
    except JointTaken as exc:
        raise ValueError(str(exc)) from exc
    clear_cache()

@st.dialog("선택한 JOINT 데이터 삭제")
def confirm_joint_delete(doc_ids: list[str], labels: dict[str, str]):
    st.warning("선택한 JOINT의 현재 상태와 연결된 모든 작업 이력이 영구 삭제됩니다. 이 작업은 되돌릴 수 없습니다.")
    for doc_id in doc_ids:
        st.write(f"• {labels.get(doc_id, doc_id)}")
    confirm, cancel = yes_no()
    if confirm:
        try:
            delete_joint_records(doc_ids)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state.pop("delete_joint_pending", None)
            st.session_state.reset_joint_status_editor = True
            st.session_state["delete_joint_msg"] = f"JOINT {len(doc_ids)}건과 연결된 이력을 삭제했습니다."
            st.rerun()
    if cancel:
        st.session_state.pop("delete_joint_pending", None)
        st.session_state.reset_joint_status_editor = True
        st.rerun()

# ---------------------------------------------------------------- shared ----
def nav_bar(current: str):
    with st.container(horizontal=True, key="navbar"):
        user = current_user()
        nav_items = [("start", "🔥 용접시작")]
        if can_manage_team() or active_segments(user["team"], user["name"]):
            nav_items.append(("finish", "✅ 용접완료"))
        if can_manage_team():
            nav_items.append(("list", "📋 용접목록"))
        for key, label in nav_items:
            clicked = st.button(
                label, key=f"nav_{key}", type="primary" if key == current else "secondary", width="stretch"
            )
            if clicked and key != current:
                st.switch_page(PAGES[key])
    user = current_user()
    if is_admin():
        display_name = "관리자"
    elif is_team_admin():
        display_name = f"팀 관리자 · {user['team']}"
    else:
        display_name = f"{user['team']} · {user['name']}"
    with st.container(horizontal=True):
        st.caption(f"로그인: {display_name}")
        if st.button("로그아웃", key="nav_logout", width="stretch"):
            st.session_state.pop("current_user", None)
            st.rerun()
    st.write("")

def pick_worker(
    key: str = "w",
    exclude: tuple[str, str] | None = None,
    fixed_team: str | None = None,
) -> tuple[str | None, str | None]:
    if fixed_team:
        team = fixed_team
    else:
        team = st.selectbox(
            "직반",
            list(WORKERS),
            index=None,
            placeholder="직반을 선택하세요",
            key=f"{key}_team",
            on_change=lambda: st.session_state.pop(f"{key}_name", None),
        )
    if not team:
        return None, None
    names = [n for n in WORKERS[team] if (team, n) != exclude]
    if st.session_state.get(f"{key}_name") not in names:
        st.session_state.pop(f"{key}_name", None)
    name = st.selectbox("이름", names, index=None, placeholder="이름을 선택하세요", key=f"{key}_name")
    return team, name

def remember_worker(team: str, name: str):
    if st.session_state.get("cookie_saved") == (team, name):
        return
    st.session_state.cookie_saved = (team, name)
    attrs = f"max-age={COOKIE_MAX_AGE}; path=/; SameSite=Lax"
    st.html(
        f"<script>document.cookie = 'welder_team={quote(team)}; {attrs}';"
        f"document.cookie = 'welder_name={quote(name)}; {attrs}';</script>",
        unsafe_allow_javascript=True,
    )


def uppercase_input(key: str):
    value = st.session_state.get(key)
    if isinstance(value, str):
        st.session_state[key] = value.upper()

def show_record(rec: dict, extra: dict | None = None):
    rows = {
        "직반": rec["직반"],
        "이름": rec["작업자"],
        "프로젝트": rec["프로젝트번호"],
        "SPOOL NO": rec["SPOOL_NO"],
        "JOINT NO": rec["JOINT_NO"],
        **(extra or {}),
    }
    st.table(pd.DataFrame({"내용": list(rows.values())}, index=list(rows.keys())))

def yes_no() -> tuple[bool, bool]:
    with st.container(horizontal=True):
        yes = st.button("예", type="primary", width="stretch")
        no = st.button("아니오", width="stretch")
    return yes, no

def joint_label(rec: dict) -> str:
    return f"SPOOL {rec['SPOOL_NO']} / JOINT {rec['JOINT_NO']}"

# ------------------------------------------------------- 용접시작 page ----
def save_start(rec: dict, ts: datetime):
    date, time = fmt(ts)
    seg = {c: None for c in LOG_COLUMNS}
    seg.update(
        id=time_ns(), 직반=rec["직반"], 작업자=rec["작업자"], 프로젝트번호=rec["프로젝트번호"], 입력방식=rec["입력방식"],
        SPOOL_NO=rec["SPOOL_NO"], TAG_NO=rec["TAG_NO"], JOINT_NO=rec["JOINT_NO"], 상태=ACTIVE,
        용접시작일자=date, 용접시작시간=time,
    )
    try:
        _start_tx(get_db().transaction(), joint_ref(seg), log_ref(seg["id"]), seg)
    except JointTaken:
        st.session_state.start_error = (
            f"JOINT {rec['JOINT_NO']}은(는) 이미 다른 작업자가 용접시작했거나 완료된 JOINT입니다. 목록을 확인하세요."
        )
        return
    clear_cache()
    st.session_state.start_result = {**rec, "date": date, "time": time}
    st.session_state.start_reset = True

@st.dialog("용접시작 확인")
def start_dialog(rec: dict, prev: dict | None, ts: datetime):
    extra = {}
    if prev:
        extra["이전 진행률"] = f"{prev['진행률'] or 0}%"
        if (prev["직반"], prev["작업자"]) != (rec["직반"], rec["작업자"]):
            extra["이전 작업자"] = f"{prev['직반']} {prev['작업자']}"
    show_record(rec, extra)
    date, time = fmt(ts)
    st.markdown(f"### {date} {time}")
    st.write("이 시각으로 용접시작을 등록하시겠습니까?")
    yes, no = yes_no()
    if yes:
        save_start(rec, ts)
        st.rerun()
    if no:
        st.rerun()

def start_controls(rec: dict, prev: dict | None, me: tuple[str, str] | None, key: str):
    worker = me
    if prev:
        prev_worker = (prev["직반"], prev["작업자"])
        st.info(
            f"⏸ 일시정지된 JOINT — 진행률 **{prev['진행률'] or 0}%** · "
            f"이전 작업자 {prev_worker[0]} {prev_worker[1]}"
        )
        if can_manage_team():
            choice = st.radio(
                "작업 방식",
                ["용접계속", "용접사 변경"],
                index=None,
                horizontal=True,
                key=f"{key}_choice",
            )
            if choice is None:
                return
            if choice == "용접계속":
                worker = prev_worker
            else:
                if me and me != prev_worker and f"{key}_team" not in st.session_state:
                    st.session_state[f"{key}_team"], st.session_state[f"{key}_name"] = me
                fixed_team = None if is_admin() else current_user()["team"]
                worker = pick_worker(key, exclude=prev_worker, fixed_team=fixed_team)
        elif prev_worker != me:
            st.error("다른 작업자의 JOINT에는 접근할 수 없습니다.")
            return
    if not worker or not worker[1]:
        return
    record = {"직반": worker[0], "작업자": worker[1], **rec}
    if st.button("용접시작", type="primary", width="stretch", key=f"go_{key}"):
        start_dialog(record, prev, now())

PROJECTS = ["SN2686", "SN2688"]
OTHER_PROJECT = "직접입력"

def start_search(me: tuple[str, str]):
    choice = st.radio("프로젝트", [*PROJECTS, OTHER_PROJECT], index=None, horizontal=True, key="s_project")
    if choice == OTHER_PROJECT:
        project = st.text_input("프로젝트 직접 입력", key="s_project_other", placeholder="프로젝트 번호 입력").strip().upper()
    else:
        project = choice
    if not project:
        return
    spool = st.text_input(
        "SPOOL NO",
        key="s_spool",
        placeholder="SPOOL NO 입력",
        on_change=uppercase_input,
        args=("s_spool",),
    ).strip().upper()
    joint = st.text_input(
        "JOINT NO",
        key="s_joint",
        placeholder="JOINT NO 입력",
        on_change=uppercase_input,
        args=("s_joint",),
    ).strip().upper()
    tag = "" 
    if not (spool and joint):
        return

    seg = latest_by_joint(project).get((spool, tag, joint))
    if (
        seg
        and seg["상태"] == PAUSED
        and not can_access_record(seg["직반"], seg["작업자"])
    ):
        st.warning("이 JOINT는 다른 작업자의 일시정지 작업입니다.")
        return
    if seg and seg["상태"] in (ACTIVE, DONE):
        if not can_access_record(seg["직반"], seg["작업자"]):
            st.warning("이미 사용할 수 없는 JOINT입니다.")
            return
        who = f" ({seg['직반']} {seg['작업자']})" if seg["상태"] == ACTIVE else ""
        st.warning(f"이미 {seg['상태']}인 JOINT입니다.{who}")
        return
    prev = seg if seg and seg["상태"] == PAUSED else None
    rec = {"프로젝트번호": project, "입력방식": INPUT_MODE, "SPOOL_NO": spool, "TAG_NO": tag, "JOINT_NO": joint}
    start_controls(rec, prev, me, key=f"c_s_{project}_{spool}_{tag}_{joint}")

def cancel_joint(prev: dict):
    try:
        _cancel_tx(get_db().transaction(), joint_ref(prev), log_ref(prev["id"]), prev["id"])
    except JointTaken:
        st.session_state.cancel_msg = ("error", "이미 다시 시작되었거나 취소된 JOINT입니다.")
        return
    clear_cache()
    st.session_state.cancel_msg = (
        "success",
        f"용접취소 등록 완료: {joint_label(prev)}  \n"
        f"일시정지까지의 진행률 {prev['진행률'] or 0}%는 기록에 저장되고, 이 JOINT는 0%부터 다시 시작합니다.",
    )

@st.dialog("용접취소 확인")
def cancel_dialog(prev: dict):
    show_record(prev, {"일시정지 진행률": f"{prev['진행률'] or 0}%"})
    st.write("일시정지까지의 진행률은 기록에 저장되고, 나머지는 입력되지 않습니다. "
             "이 JOINT의 진행률은 0%로 돌아갑니다. 용접취소하시겠습니까?")
    yes, no = yes_no()
    if yes:
        cancel_joint(prev)
        st.rerun()
    if no:
        st.rerun()

def paused_section(me: tuple[str, str] | None):
    rows = paused_joints()
    if not is_admin():
        if is_team_admin():
            rows = [r for r in rows if r["직반"] == current_user()["team"]]
        else:
            rows = [r for r in rows if (r["직반"], r["작업자"]) == me]
    st.subheader(f"⏸ 일시정지 목록 ({len(rows)}건)")
    if not rows:
        return

    if can_manage_team():
        rows.sort(key=lambda r: (r["직반"], r["작업자"]) != me)
    table = pd.DataFrame(
        {
            "이름": r["작업자"],
            "SPOOL NO": r["SPOOL_NO"],
            "JOINT NO": r["JOINT_NO"],
            "진행률": r["진행률"] or 0,
            "직반": r["직반"],
            "일시정지": f"{r['일시정지일자']} {r['일시정지시간']}",
            "프로젝트": r["프로젝트번호"],
        }
        for r in rows
    )
    event = st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"tbl_paused_{hash(tuple(r['id'] for r in rows))}",
        column_config={
            "진행률": st.column_config.ProgressColumn("진행률", format="%d%%", min_value=0, max_value=100)
        },
    )
    if not event.selection.rows:
        return

    prev = rows[event.selection.rows[0]]
    st.markdown(f"**선택:** {joint_label(prev)}")
    if st.button("🚫 용접취소", width="stretch", key=f"cancel_{prev['id']}"):
        cancel_dialog(prev)
    rec = {k: prev[k] for k in ("프로젝트번호", "입력방식", "SPOOL_NO", "TAG_NO", "JOINT_NO")}
    start_controls(rec, prev, me, key=f"c_p_{prev['id']}")

def start_page():
    nav_bar("start")
    if notice := st.session_state.pop("start_notice", None):
        (st.success if notice[0] == "success" else st.error)(notice[1])
    if st.session_state.pop("start_reset", False):
        for key in list(st.session_state):
            if key in ("s_spool", "s_joint") or key.startswith("c_"):
                del st.session_state[key]
    if res := st.session_state.pop("start_result", None):
        st.success(
            f"용접시작 등록 완료: **{res['date']} {res['time']}**  \n"
            f"{res['직반']} {res['작업자']} · {joint_label(res)}"
        )
        user = current_user()
        if user["role"] == "worker" and active_segments(user["team"], user["name"]):
            st.switch_page(PAGES["finish"])
    if msg := st.session_state.pop("cancel_msg", None):
        (st.success if msg[0] == "success" else st.error)(msg[1])
    if err := st.session_state.pop("start_error", None):
        st.error(err)

    user = current_user()
    if is_admin():
        team, name = pick_worker()
        me = (team, name) if name else None
    elif is_team_admin():
        team = user["team"]
        _, name = pick_worker(fixed_team=team)
        me = (team, name) if name else None
    else:
        team, name = user["team"], user["name"]
        me = (team, name)
    if me:
        if is_admin():
            remember_worker(*me)
        start_search(me)

    st.divider()
    paused_section(me)

# ------------------------------------------------------- 용접완료 page ----
def end_segment(rec: dict, ts: datetime, status: str, progress: int):
    if status == PAUSED and progress > 90:
        st.session_state.finish_msg = (
            "error",
            "일시정지 진행률은 최대 90%입니다. 100% 완료는 용접완료 버튼으로 등록하세요.",
        )
        return
    date, time = fmt(ts)
    date_col, time_col = ("일시정지일자", "일시정지시간") if status == PAUSED else ("용접완료일자", "용접완료시간")
    changes = {"상태": status, "진행률": progress, date_col: date, time_col: time}
    try:
        _end_tx(get_db().transaction(), joint_ref(rec), log_ref(rec["id"]), rec["id"], changes, progress)
    except JointTaken:
        st.session_state.finish_msg = ("error", "이미 일시정지 또는 완료 처리된 JOINT입니다.")
    else:
        clear_cache()
        if status == PAUSED:
            st.session_state.finish_msg = (
                "success",
                f"일시정지 등록 완료: **{date} {time}** · 진행률 **{progress}%**  \n{joint_label(rec)}  \n"
                "용접시작 페이지의 일시정지 목록에서 다시 시작할 수 있습니다.",
            )
        else:
            st.session_state.finish_msg = ("success", f"용접완료 등록 완료: **{date} {time}**  \n{joint_label(rec)}")
    st.session_state.pop("f_record", None)

@st.dialog("용접완료 확인")
def finish_dialog(rec: dict, ts: datetime):
    show_record(rec, {"용접시작": f"{rec['용접시작일자']} {rec['용접시작시간']}"})
    date, time = fmt(ts)
    st.markdown(f"### {date} {time}")
    st.write("이 시각으로 용접완료를 등록하시겠습니까?")
    yes, no = yes_no()
    if yes:
        end_segment(rec, ts, DONE, 100)
        st.rerun()
    if no:
        st.rerun()

@st.dialog("일시정지 확인")
def pause_dialog(rec: dict, min_progress: int, ts: datetime):
    show_record(rec, {"용접시작": f"{rec['용접시작일자']} {rec['용접시작시간']}"})
    date, time = fmt(ts)
    st.markdown(f"### {date} {time}")
    if min_progress > 90:
        st.warning("현재 저장된 진행률이 90%를 초과합니다. 추가 일시정지 대신 용접완료를 등록하세요.")
        return
    elif min_progress == 90:
        progress = 90
    else:
        progress = st.slider(
            "진행률", min_value=min_progress, max_value=90, value=min_progress, step=10, format="%d%%"
        )
    st.write("이 시각으로 일시정지를 등록하시겠습니까?")
    yes, no = yes_no()
    if yes:
        end_segment(rec, ts, PAUSED, progress)
        st.rerun()
    if no:
        st.rerun()

def my_active_section(team: str, name: str):
    records = {r["id"]: r for r in active_segments(team, name)}
    if not records:
        st.info("진행중인 JOINT가 없습니다.")
        return

    ids = list(records)
    if st.session_state.get("f_record") not in ids:
        st.session_state.pop("f_record", None)
    rid = st.radio(
        f"진행중인 JOINT ({len(ids)}건) — 항목을 선택하세요",
        ids,
        index=None,
        format_func=lambda i: f"SPOOL {records[i]['SPOOL_NO']}  |  JOINT {records[i]['JOINT_NO']}",
        key="f_record",
    )
    if rid is None:
        return
    with st.container(horizontal=True):
        pause = st.button("⏸ 일시정지", width="stretch", key="f_pause")
        done = st.button("용접완료", type="primary", width="stretch", key="f_done")
    if pause:
        pause_dialog(records[rid], joint_progress(records[rid]), now())
    if done:
        finish_dialog(records[rid], now())

def active_list_section(me: tuple[str, str] | None = None, team_scope: str | None = None):
    if me:
        rows = active_segments(*me)
    else:
        rows = active_segments(team=team_scope)
    st.subheader(f"🔧 진행중 목록 ({len(rows)}건)")
    if not rows:
        st.caption("진행중인 JOINT가 없습니다.")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "직반": r["직반"],
                "이름": r["작업자"],
                "SPOOL NO": r["SPOOL_NO"],
                "JOINT NO": r["JOINT_NO"],
                "시작일": r["용접시작일자"],
                "시작시간": r["용접시작시간"],
            }
            for r in rows
        ),
        hide_index=True,
        width="stretch",
    )

def finish_page():
    user = current_user()
    if not can_manage_team() and not active_segments(user["team"], user["name"]):
        if notice := st.session_state.pop("finish_msg", None):
            st.session_state.start_notice = notice
        st.switch_page(PAGES["start"])

    nav_bar("finish")
    if msg := st.session_state.pop("finish_msg", None):
        (st.success if msg[0] == "success" else st.error)(msg[1])
        if msg[0] == "success" and st.button(
            "🔥 용접시작으로 돌아가기",
            type="primary",
            width="stretch",
            key="return_to_start_after_finish",
        ):
            st.switch_page(PAGES["start"])

    if is_admin():
        team, name = pick_worker()
        me = (team, name) if name else None
        if me:
            remember_worker(*me)
            my_active_section(team, name)
    elif is_team_admin():
        team = user["team"]
        _, name = pick_worker(key="f", fixed_team=team)
        me = (team, name) if name else None
        if me:
            my_active_section(team, name)
    else:
        team, name = user["team"], user["name"]
        me = (team, name)
        my_active_section(team, name)

    st.divider()
    if is_admin():
        active_list_section()
    elif is_team_admin():
        active_list_section(team_scope=user["team"])
    else:
        active_list_section(me)

# ------------------------------------------------------- 용접목록 page ----
def to_excel(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="용접현황")
        sheet = writer.sheets["용접현황"]
        for col_cells in sheet.columns:
            width = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
            sheet.column_dimensions[col_cells[0].column_letter].width = min(max(width * 1.3 + 2, 8), 40)
    return buf.getvalue()

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

def send_excel_email(recipient: str, filename: str, data: bytes):
    """Send an Excel attachment from the configured Gmail account over SMTP/TLS."""
    email_settings = st.secrets.get("email", {})
    sender = str(email_settings.get("gmail_address", "")).strip()
    app_password = re.sub(r"\s+", "", str(email_settings.get("gmail_app_password", "")))
    if not sender or not app_password:
        raise ValueError(
            "Gmail 발송 설정이 없습니다. Streamlit Secrets의 [email] 설정을 확인하세요."
        )

    message = EmailMessage()
    message["Subject"] = f"용접 실적 Excel 파일: {filename}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("요청하신 용접 실적 Excel 파일을 첨부합니다.")
    message.add_attachment(
        data,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(sender, app_password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise ValueError(
            "Gmail 로그인이 거부되었습니다. Gmail 주소와 Google App Password를 확인하세요."
        ) from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise ValueError(
            "Gmail 서버에 연결하거나 메일을 보내지 못했습니다. 설정을 확인하고 다시 시도하세요."
        ) from exc

def list_page():
    if not can_manage_team():
        st.switch_page(PAGES["start"])
    if st.session_state.pop("reset_joint_status_editor", False):
        st.session_state.pop("joint_status_editor", None)

    nav_bar("list")
    if st.button("🔄 새로고침", key="l_refresh"):
        clear_cache()
    df = load_log()
    if df.empty:
        st.info("저장된 데이터가 없습니다.")
        return

    df = df.sort_values("id").reset_index(drop=True) 
    if not is_admin():
        user = current_user()
        if is_team_admin():
            df = df[df["직반"] == user["team"]].copy()
        else:
            df = df[(df["직반"] == user["team"]) & (df["작업자"] == user["name"])].copy()
        if df.empty:
            st.info("저장된 작업 기록이 없습니다.")
            return

    start = pd.to_datetime(df["용접시작일자"].str.cat(df["용접시작시간"], sep=" "))
    end_date = df["용접완료일자"].fillna(df["일시정지일자"])
    end_time = df["용접완료시간"].fillna(df["일시정지시간"])
    end = pd.to_datetime(end_date.str.cat(end_time, sep=" "))
    df["소요시간(분)"] = ((end - start).dt.total_seconds() / 60).round(1)
    cancelled = df["상태"] == CANCELED
    df["_cycle"] = cancelled.groupby([df[c] for c in JOINT_COLS]).cumsum() - cancelled
    cycle = df.groupby([*JOINT_COLS, "_cycle"])
    df["JOINT 누적(분)"] = cycle["소요시간(분)"].transform(lambda s: s.sum(min_count=1)).round(1)
    joint_minutes = (
        df.sort_values("id")
        .groupby(list(JOINT_COLS), dropna=False)["JOINT 누적(분)"]
        .last()
        .to_dict()
    )
    df["누계 진행률(%)"] = df["진행률"].astype("Int64")
    df["당일 진행률(%)"] = (df["진행률"] - cycle["진행률"].shift().fillna(0)).astype("Int64")
    df["_sort_start"] = start
    df = df.sort_values(["_sort_start", "id"], ascending=[False, False], kind="stable")
    start = df["_sort_start"]

    joint_states = visible_joint_states()
    summary_rows = []
    for row in joint_states:
        state = row.get("상태", "")
        progress = 100 if state == DONE else int(row.get("base_progress") or 0)
        summary_rows.append({
            "_doc_id": row["_doc_id"],
            "상태": state,
            "직반": row.get("직반", ""),
            "작업자": row.get("작업자", ""),
            "프로젝트번호": row.get("프로젝트번호", ""),
            "SPOOL_NO": row.get("SPOOL_NO", ""),
            "JOINT_NO": row.get("JOINT_NO", ""),
            "누계 진행률(%)": progress,
            "JOINT 누적(분)": joint_minutes.get(
                tuple(row.get(column) for column in JOINT_COLS), 0
            ),
            "용접시작일자": row.get("용접시작일자") or "",
            "용접시작시간": row.get("용접시작시간") or "",
            "용접완료일자": row.get("용접완료일자") or "",
            "용접완료시간": row.get("용접완료시간") or "",
        })
    summary_df = pd.DataFrame(summary_rows)
    if summary_df.empty:
        st.info("JOINT 상태 데이터가 없습니다.")
        return
    summary_start = pd.to_datetime(
        summary_df["용접시작일자"].astype(str) + " " + summary_df["용접시작시간"].astype(str),
        errors="coerce",
    )
    today = now().date()
    recent_start = today - timedelta(days=2)

    with st.expander("필터", expanded=False):
        if is_admin():
            c1, c2 = st.columns(2)
            team = c1.selectbox("직반", ["전체", *WORKERS], key="l_team")
            names = sorted(summary_df["작업자"].unique()) if team == "전체" else sorted(
                summary_df.loc[summary_df["직반"] == team, "작업자"].unique()
            )
            name = c2.selectbox("이름", ["전체", *names], key=f"l_name_{team}")
        elif is_team_admin():
            team = user["team"]
            c1, c2 = st.columns(2)
            c1.text_input("직반", value=team, disabled=True)
            names = sorted(summary_df.loc[summary_df["직반"] == team, "작업자"].unique())
            name = c2.selectbox("이름", ["전체", *names], key=f"l_name_{team}")
        else:
            team, name = user["team"], user["name"]
        status = st.radio("상태", ["전체", ACTIVE, PAUSED, DONE, CANCELED], horizontal=True, key="l_status")
        valid_start = summary_start.dropna()
        first, last = (
            (valid_start.min().date(), valid_start.max().date())
            if not valid_start.empty else (start.min().date(), start.max().date())
        )
        default_period = (recent_start, today) if is_team_admin() else (first, last)
        period = st.date_input("용접시작일자 기간", default_period, key="l_period")
        search = st.text_input("SPOOL / JOINT 검색", key="l_search").strip().lower()

    mask = pd.Series(True, index=df.index)
    if team != "전체":
        mask &= df["직반"] == team
    if name != "전체":
        mask &= df["작업자"] == name
    if status != "전체":
        mask &= df["상태"] == status
    if isinstance(period, tuple) and len(period) == 2:
        mask &= start.dt.date.between(period[0], period[1])
    if search:
        mask &= df[["SPOOL_NO", "JOINT_NO"]].apply(
            lambda c: c.str.lower().str.contains(search, regex=False)
        ).any(axis=1)
    history_shown = df[mask]

    summary_mask = pd.Series(True, index=summary_df.index)
    if team != "전체":
        summary_mask &= summary_df["직반"] == team
    if name != "전체":
        summary_mask &= summary_df["작업자"] == name
    if status != "전체":
        summary_mask &= summary_df["상태"] == status
    if isinstance(period, tuple) and len(period) == 2:
        date_mask = summary_start.dt.date.between(period[0], period[1])
        if is_team_admin() and period == default_period:
            date_mask |= summary_df["상태"].isin([ACTIVE, PAUSED])
        summary_mask &= date_mask
    if search:
        summary_mask &= summary_df[["SPOOL_NO", "JOINT_NO"]].apply(
            lambda column: column.astype(str).str.lower().str.contains(search, regex=False)
        ).any(axis=1)
    shown_summary = summary_df[summary_mask].copy()
    shown_summary["_sort_start"] = summary_start.loc[shown_summary.index]
    shown_summary = shown_summary.sort_values("_sort_start", ascending=False, kind="stable").drop(
        columns="_sort_start"
    )

    with st.container(horizontal=True):
        st.metric("JOINT", f"{len(shown_summary):,}")
        st.metric("진행중", f"{(shown_summary['상태'] == ACTIVE).sum():,}")
        st.metric("일시정지", f"{(shown_summary['상태'] == PAUSED).sum():,}")
        st.metric("완료", f"{(shown_summary['상태'] == DONE).sum():,}")

    summary_columns = ["상태", "작업자", "프로젝트번호", "SPOOL_NO", "JOINT_NO", "누계 진행률(%)",
                       "JOINT 누적(분)", "직반", "용접시작일자", "용접시작시간", "용접완료일자", "용접완료시간"]
    st.subheader("용접현황")
    table_rows = shown_summary[["_doc_id", *summary_columns]].copy().reset_index(drop=True)
    table_rows["삭제 선택"] = False

    def status_row_style(row):
        if row["상태"] == ACTIVE:
            color = "#fff2cc"
        elif row["상태"] == DONE:
            color = "#d9ead3"
        else:
            color = ""
        return [f"background-color: {color}" if color else ""]

    edited_table = st.data_editor(
        table_rows.style.apply(status_row_style, axis=1, subset=["상태"]),
        hide_index=True,
        width="stretch",
        height=750,
        num_rows="fixed",
        key="joint_status_editor",
        disabled=["_doc_id", "상태", "직반", "작업자", "프로젝트번호", "누계 진행률(%)",
                  "JOINT 누적(분)", "용접시작일자", "용접시작시간", "용접완료일자", "용접완료시간"],
        column_config={
            "_doc_id": None,
            "프로젝트번호": "프로젝트",
            "SPOOL_NO": "SPOOL NO",
            "JOINT_NO": "JOINT NO",
            "삭제 선택": st.column_config.CheckboxColumn("삭제 선택"),
            "SPOOL_NO": st.column_config.TextColumn("SPOOL_NO"),
            "JOINT_NO": st.column_config.TextColumn("JOINT_NO"),
            "누계 진행률(%)": st.column_config.ProgressColumn("누계 진행률(%)", format="%d%%", min_value=0, max_value=100),
            "JOINT 누적(분)": st.column_config.NumberColumn("JOINT 누적(분)", format="%.1f"),
        },
    )

    if msg := st.session_state.pop("joint_table_msg", None):
        st.success(msg)
    if msg := st.session_state.pop("joint_table_error", None):
        st.error(msg)

    states_by_id = {row["_doc_id"]: row for row in joint_states}
    if st.button("SPOOL / JOINT 수정 저장", type="primary", key="save_joint_table_edits"):
        saved = 0
        errors = []
        for _, edited in edited_table.iterrows():
            doc_id = edited["_doc_id"]
            original = states_by_id.get(doc_id)
            if not original:
                errors.append("표의 JOINT 상태가 변경되었습니다. 새로고침 후 다시 시도하세요.")
                continue
            spool_value, joint_value = edited["SPOOL_NO"], edited["JOINT_NO"]
            spool = "" if pd.isna(spool_value) else str(spool_value).strip().upper()
            joint_no = "" if pd.isna(joint_value) else str(joint_value).strip().upper()
            if spool == str(original.get("SPOOL_NO") or "").strip() and joint_no == str(original.get("JOINT_NO") or "").strip():
                continue
            try:
                rename_joint(original, spool, joint_no)
            except ValueError as exc:
                errors.append(f"{joint_label(original)}: {exc}")
            else:
                saved += 1
        st.session_state.reset_joint_status_editor = True
        if saved:
            st.session_state.joint_table_msg = f"{saved}건의 SPOOL / JOINT 번호를 수정하고 연결된 전체 이력에 반영했습니다."
        if errors:
            st.session_state.joint_table_error = "\n".join(errors)
        st.rerun()

    labels = {
        row["_doc_id"]: (
            f"{row.get('프로젝트번호', '')} · {row.get('직반', '')} {row.get('작업자', '')} · "
            f"SPOOL {row.get('SPOOL_NO', '')} / JOINT {row.get('JOINT_NO', '')} · "
            f"{row.get('상태', '')} · {100 if row.get('상태') == DONE else row.get('base_progress', 0) or 0}%"
        )
        for row in joint_states
    }
    selected_delete_ids = edited_table.loc[edited_table["삭제 선택"], "_doc_id"].tolist()
    if st.button("🗑️ 선택한 JOINT와 전체 이력 삭제", disabled=not selected_delete_ids, type="secondary"):
        st.session_state.delete_joint_pending = list(selected_delete_ids)
        st.rerun()
    if msg := st.session_state.pop("delete_joint_msg", None):
        st.success(msg)
    if pending := st.session_state.get("delete_joint_pending"):
        confirm_joint_delete(pending, labels)

    with st.expander("작업 이력 상세 · 진행률 입력 기록"):
        st.dataframe(
            history_shown[["상태", "직반", "작업자", "프로젝트번호", "SPOOL_NO", "JOINT_NO", "누계 진행률(%)", "당일 진행률(%)",
                           "용접시작일자", "용접시작시간", "일시정지일자", "일시정지시간", "용접완료일자", "용접완료시간",
                           "소요시간(분)", "JOINT 누적(분)"]],
            hide_index=True,
            width="stretch",
            column_config={
                "누계 진행률(%)": st.column_config.NumberColumn(format="%d%%"),
                "당일 진행률(%)": st.column_config.NumberColumn(format="%d%%"),
            },
        )

    all_export = summary_df[summary_columns].copy()
    all_export["_sort_start"] = summary_start
    all_export = all_export.sort_values("_sort_start", ascending=False, kind="stable").drop(
        columns="_sort_start"
    )
    all_export = all_export.rename(
        columns={"프로젝트번호": "프로젝트", "SPOOL_NO": "SPOOL NO", "JOINT_NO": "JOINT NO"}
    )

    stamp = now().strftime("%Y%m%d_%H%M")
    st.subheader("다운로드")
    all_filename = f"welding_status_{stamp}.xlsx"
    all_data = to_excel(all_export)

    st.download_button(
        "📥 전체 데이터 Excel",
        all_data,
        file_name=all_filename,
        mime=XLSX_MIME,
        width="stretch",
    )

    st.subheader("이메일로 보내기")
    email_recipient = st.text_input(
        "받는 이메일 주소",
        key="email_recipient",
        placeholder="name@example.com",
    ).strip()
    send_all = st.button("📧 전체 Excel 보내기", width="stretch", key="send_all_email")

    if send_all:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email_recipient):
            st.error("올바른 이메일 주소를 입력하세요.")
        else:
            try:
                send_excel_email(email_recipient, all_filename, all_data)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"{email_recipient} 주소로 Excel 파일을 보냈습니다.")

# ---------------------------------------------------------------- app ----
if not st.session_state.get("current_user"):
    login_page()
    st.stop()

get_db()
user = current_user()
landing_page = "start"
if user["role"] == "worker" and active_segments(user["team"], user["name"]):
    landing_page = "finish"

PAGES = {
    "start": st.Page(start_page, title="용접시작", icon="🔥", default=landing_page == "start"),
    "finish": st.Page(finish_page, title="용접완료", icon="✅", url_path="finish", default=landing_page == "finish"),
    "list": st.Page(list_page, title="용접목록", icon="📋", url_path="list"),
}
available_pages = [PAGES["start"], PAGES["finish"]]
if can_manage_team():
    available_pages.append(PAGES["list"])
st.navigation(available_pages, position="hidden").run()
