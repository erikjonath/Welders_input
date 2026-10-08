import hashlib
from datetime import datetime, timedelta
from io import BytesIO
from time import time_ns
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

KST = ZoneInfo("Asia/Seoul")  # the cloud server runs on UTC; welders work in Korean time
CACHE_TTL = 15  # seconds a Firestore read is reused (a write from this app clears it at once)
COOKIE_MAX_AGE = 365 * 24 * 3600  # remember 직반 / 이름 on the phone for a year

WORKERS = {
    "1직1반": ["라흐마뚤러", "에릭", "짜효노", "누로흐만", "리스키갈리", "데바", "수얏노", "에펜디", "조디", "마르또노"],
    "1직2반": ["쁘리아트모노", "위자나르코", "나우팔", "앙고로", "누르콜릭", "헤르만토", "마난", "리야디", "루이스"],
    "1직3반": ["바유함자", "아프릴라", "데디", "사뿌트라", "무지오노", "수다르모노", "유누스", "푸풍", "아흐마드샤피", "안자스"],
    "1직4반": ["라마", "샤리푸딘", "시딕", "알피안디", "카릴", "파이잘", "푸트라", "프라무디아", "헨드리"],
}

# Each row in welding_log is one work segment by one welder; 상태 says how the segment ended.
# 취소 = a paused segment whose joint was cancelled: its progress stays on record, but the joint starts again from 0%.
ACTIVE, PAUSED, DONE, CANCELED = "진행중", "일시정지", "완료", "취소"
INPUT_MODE = "직접입력"  # SPOOL / JOINT numbers are always typed by the welder
JOINT_COLS = ("프로젝트번호", "SPOOL_NO", "TAG_NO", "JOINT_NO")
LOG_COLUMNS = ["id", "직반", "작업자", "프로젝트번호", "입력방식", "SPOOL_NO", "TAG_NO", "JOINT_NO", "상태", "진행률",
               "용접시작일자", "용접시작시간", "일시정지일자", "일시정지시간", "용접완료일자", "용접완료시간"]

st.set_page_config(page_title="용접 실적 입력", page_icon="🔥", layout="centered")

# Streamlit drops widget values when a widget is not drawn (other page / hidden step).
# Re-assigning them keeps 직반, 이름 and the start form filled in when the user comes back.
for _key in list(st.session_state.keys()):
    if _key.startswith(("w_", "s_", "c_")):
        st.session_state[_key] = st.session_state[_key]

# New session on this phone: restore the 직반 / 이름 it used last time.
if "cookie_checked" not in st.session_state:
    st.session_state.cookie_checked = True
    _team = unquote(st.context.cookies.get("welder_team", ""))
    _name = unquote(st.context.cookies.get("welder_name", ""))
    if _team in WORKERS:
        st.session_state.w_team = _team
        if _name in WORKERS[_team]:
            st.session_state.w_name = _name
            st.session_state.cookie_saved = (_team, _name)

# Replace these imports at the top if necessary:
import firebase_admin
from firebase_admin import credentials, firestore as firebase_firestore

# Update your client getter functions:
@st.cache_resource
def _client():
    if not firebase_admin._apps:
        cred = credentials.Certificate(dict(st.secrets["firebase"]))
        firebase_admin.initialize_app(cred)
    return firebase_firestore.client()

def get_db():
    try:
        return _client()
    except Exception as e:
        st.error(f"Firebase에 연결할 수 없습니다. Secrets의 [firebase] 설정을 확인하세요. ({type(e).__name__})")
        st.stop()


# ---------------------------------------------------------------- data ----
def fmt(ts: datetime) -> tuple[str, str]:
    return ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M:%S")


# ------------------------------------------------------------ database ----
# Firestore layout:
#   welding_log/{id}  one document per work segment of one welder (the full history, read by 용접목록)
#   joints/{hash}     one document per joint: a copy of its latest segment plus `base_progress`
#                     (진행률 reached so far in the current cycle; a 용접취소 resets it to 0).
# The joints document is what makes "only one 진행중/완료 segment per joint" safe: it is read and
# written inside a transaction, so two welders cannot start the same joint at the same moment.
class JointTaken(Exception):
    """The joint changed state while the user was looking at it."""


@st.cache_resource
def _client() -> firestore.Client:
    return firestore.Client.from_service_account_info(dict(st.secrets["firebase"]))


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
    for fn in (latest_by_joint, paused_joints, _active_all, load_log):
        fn.clear()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def latest_by_joint(project: str) -> dict[tuple[str, str, str], dict]:
    """Latest segment of every joint in the project that has been started at least once."""
    docs = get_db().collection("joints").where(filter=FieldFilter("프로젝트번호", "==", project)).stream()
    return {(d["SPOOL_NO"], d["TAG_NO"], d["JOINT_NO"]): d for d in (x.to_dict() for x in docs)}


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def paused_joints() -> list[dict]:
    """Joints whose latest segment is paused, i.e. waiting for someone to continue."""
    docs = get_db().collection("joints").where(filter=FieldFilter("상태", "==", PAUSED)).stream()
    rows = [d.to_dict() for d in docs]
    rows.sort(key=lambda r: (r["일시정지일자"], r["일시정지시간"]), reverse=True)
    return rows


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _active_all() -> list[dict]:
    docs = get_db().collection("joints").where(filter=FieldFilter("상태", "==", ACTIVE)).stream()
    rows = [d.to_dict() for d in docs]
    rows.sort(key=lambda r: (r["용접시작일자"], r["용접시작시간"]))
    return rows


def active_segments(team: str | None = None, name: str | None = None) -> list[dict]:
    rows = _active_all()
    return [r for r in rows if r["직반"] == team and r["작업자"] == name] if name else rows


def joint_progress(rec: dict) -> int:
    """진행률 already reached by earlier (paused) segments of this joint."""
    return (joint_ref(rec).get().to_dict() or {}).get("base_progress", 0)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_log() -> pd.DataFrame:
    rows = [d.to_dict() for d in get_db().collection("welding_log").stream()]
    df = pd.DataFrame(rows).reindex(columns=LOG_COLUMNS)
    df["진행률"] = pd.to_numeric(df["진행률"])
    return df


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


get_db()  # stop early with a clear message when the Firebase secrets are missing


# ---------------------------------------------------------------- shared ----
def nav_bar(current: str):
    with st.container(horizontal=True, key="navbar"):
        for key, label in [("start", "🔥 용접시작"), ("finish", "✅ 용접완료"), ("list", "📋 용접목록")]:
            clicked = st.button(
                label, key=f"nav_{key}", type="primary" if key == current else "secondary", width="stretch"
            )
            if clicked and key != current:
                st.switch_page(PAGES[key])
    st.write("")


def pick_worker(key: str = "w", exclude: tuple[str, str] | None = None) -> tuple[str | None, str | None]:
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
    """Store 직반 / 이름 in a cookie so this phone is filled in automatically next time."""
    if st.session_state.get("cookie_saved") == (team, name):
        return
    st.session_state.cookie_saved = (team, name)
    attrs = f"max-age={COOKIE_MAX_AGE}; path=/; SameSite=Lax"
    st.html(
        f"<script>document.cookie = 'welder_team={quote(team)}; {attrs}';"
        f"document.cookie = 'welder_name={quote(name)}; {attrs}';</script>",
        unsafe_allow_javascript=True,
    )


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
    """Choose who welds (continue / change welder for a paused joint), then the 용접시작 button."""
    worker = me
    if prev:
        prev_worker = (prev["직반"], prev["작업자"])
        st.info(
            f"⏸ 일시정지된 JOINT — 진행률 **{prev['진행률'] or 0}%** · "
            f"이전 작업자 {prev_worker[0]} {prev_worker[1]}"
        )
        choice = st.radio(
            "작업 방식",
            ["용접계속", "용접사 변경"],
            index=None,
            horizontal=True,
            captions=[f"{prev_worker[0]} {prev_worker[1]}", "다른 용접사가 이어서 작업"],
            key=f"{key}_choice",
        )
        if choice is None:
            return
        if choice == "용접계속":
            worker = prev_worker
        else:
            if me and me != prev_worker and f"{key}_team" not in st.session_state:
                st.session_state[f"{key}_team"], st.session_state[f"{key}_name"] = me
            st.caption("이어서 작업할 용접사를 선택하세요.")
            worker = pick_worker(key, exclude=prev_worker)
    if not worker or not worker[1]:
        return
    record = {"직반": worker[0], "작업자": worker[1], **rec}
    if st.button("용접시작", type="primary", width="stretch", key=f"go_{key}"):
        start_dialog(record, prev, now())


PROJECTS = ["SN2686", "SN2688"]
OTHER_PROJECT = "직접입력"


def start_search(me: tuple[str, str]):
    """Project (2 defaults or typed), SPOOL NO and JOINT NO; typed values are stored in upper case."""
    choice = st.radio("프로젝트", [*PROJECTS, OTHER_PROJECT], index=None, horizontal=True, key="s_project")
    if choice == OTHER_PROJECT:
        project = st.text_input("프로젝트 직접 입력", key="s_project_other", placeholder="프로젝트 번호 입력").strip().upper()
    else:
        project = choice
    if not project:
        return
    spool = st.text_input("SPOOL NO", key="s_spool", placeholder="SPOOL NO 입력").strip().upper()
    joint = st.text_input("JOINT NO", key="s_joint", placeholder="JOINT NO 입력").strip().upper()
    tag = ""  # TAG NO is no longer used; kept as an empty field so the stored joint key stays the same
    if not (spool and joint):
        return

    seg = latest_by_joint(project).get((spool, tag, joint))
    if seg and seg["상태"] in (ACTIVE, DONE):
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
    st.subheader(f"⏸ 일시정지 목록 ({len(rows)}건)")
    if not rows:
        st.caption("일시정지된 JOINT가 없습니다.")
        return

    rows.sort(key=lambda r: (r["직반"], r["작업자"]) != me)  # this welder's joints first
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
    st.caption("항목을 선택하면 용접계속 또는 용접사 변경 후 다시 시작할 수 있습니다.")
    event = st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        # New key whenever the list changes, so a stale row selection never points at another joint.
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
    if st.session_state.pop("start_reset", False):
        # Keep 직반 / 이름 / 프로젝트 for the next joint; clear SPOOL NO, JOINT NO and the choices.
        for key in list(st.session_state):
            if key in ("s_spool", "s_joint") or key.startswith("c_"):
                del st.session_state[key]
    if res := st.session_state.pop("start_result", None):
        st.success(
            f"용접시작 등록 완료: **{res['date']} {res['time']}**  \n"
            f"{res['직반']} {res['작업자']} · {joint_label(res)}"
        )
    if msg := st.session_state.pop("cancel_msg", None):
        (st.success if msg[0] == "success" else st.error)(msg[1])
    if err := st.session_state.pop("start_error", None):
        st.error(err)

    team, name = pick_worker()
    me = (team, name) if name else None
    if me:
        remember_worker(*me)
        start_search(me)

    st.divider()
    paused_section(me)


# ------------------------------------------------------- 용접완료 page ----
def end_segment(rec: dict, ts: datetime, status: str, progress: int):
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
    if min_progress >= 100:
        st.warning("진행률이 이미 100%입니다. 용접완료를 등록하세요.")
        progress = 100
    else:
        progress = st.slider(
            "진행률", min_value=min_progress, max_value=100, value=min_progress, step=10, format="%d%%"
        )
        if min_progress:
            st.caption(f"이전 진행률이 {min_progress}%이므로 {min_progress}% ~ 100% 사이로 입력합니다.")
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
        captions=[
            f"프로젝트 {records[i]['프로젝트번호']} · "
            f"시작 {records[i]['용접시작일자']} {records[i]['용접시작시간']}"
            for i in ids
        ],
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


def active_list_section():
    rows = active_segments()
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
    nav_bar("finish")
    if msg := st.session_state.pop("finish_msg", None):
        (st.success if msg[0] == "success" else st.error)(msg[1])

    team, name = pick_worker()
    if name:
        remember_worker(team, name)
        my_active_section(team, name)

    st.divider()
    active_list_section()


# ------------------------------------------------------- 용접목록 page ----
def to_excel(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="용접목록")
        sheet = writer.sheets["용접목록"]
        for col_cells in sheet.columns:
            width = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
            sheet.column_dimensions[col_cells[0].column_letter].width = min(max(width * 1.3 + 2, 8), 40)
    return buf.getvalue()


def list_page():
    nav_bar("list")
    if st.button("🔄 새로고침", key="l_refresh"):
        clear_cache()
    df = load_log()
    if df.empty:
        st.info("저장된 데이터가 없습니다.")
        return

    df = df.sort_values("id").reset_index(drop=True)  # oldest first, so progress can be compared with the previous segment
    start = pd.to_datetime(df["용접시작일자"].str.cat(df["용접시작시간"], sep=" "))
    end_date = df["용접완료일자"].fillna(df["일시정지일자"])
    end_time = df["용접완료시간"].fillna(df["일시정지시간"])
    end = pd.to_datetime(end_date.str.cat(end_time, sep=" "))
    df["소요시간(분)"] = ((end - start).dt.total_seconds() / 60).round(1)
    joint = df.groupby(list(JOINT_COLS))
    # A 용접취소 restarts the joint at 0%, so progress and time are counted per cycle between cancellations.
    cancelled = df["상태"] == CANCELED
    df["_cycle"] = cancelled.groupby([df[c] for c in JOINT_COLS]).cumsum() - cancelled
    cycle = df.groupby([*JOINT_COLS, "_cycle"])
    df["JOINT 누적(분)"] = cycle["소요시간(분)"].transform(lambda s: s.sum(min_count=1)).round(1)
    df["누계 진행률(%)"] = df["진행률"].astype("Int64")
    df["당일 진행률(%)"] = (df["진행률"] - cycle["진행률"].shift().fillna(0)).astype("Int64")
    is_latest = df["id"] == joint["id"].transform("max")
    start, is_latest = start[::-1], is_latest[::-1]
    df = df.iloc[::-1]  # newest first

    with st.expander("필터", expanded=True):
        c1, c2 = st.columns(2)
        team = c1.selectbox("직반", ["전체", *WORKERS], key="l_team")
        names = sorted(df["작업자"].unique()) if team == "전체" else WORKERS[team]
        name = c2.selectbox("이름", ["전체", *names], key=f"l_name_{team}")
        status = st.radio("상태", ["전체", ACTIVE, PAUSED, DONE, CANCELED], horizontal=True, key="l_status")
        first, last = start.min().date(), start.max().date()
        period = st.date_input("용접시작일자 기간", (first, last), key="l_period")
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
    shown = df[mask]

    with st.container(horizontal=True):
        st.metric("기록", f"{len(shown):,}")
        st.metric("진행중", f"{(shown['상태'] == ACTIVE).sum():,}")
        st.metric("일시정지", f"{((shown['상태'] == PAUSED) & is_latest[mask]).sum():,}", help="현재 일시정지 상태인 JOINT")
        st.metric("완료", f"{(shown['상태'] == DONE).sum():,}")

    columns = ["상태", "직반", "작업자", "프로젝트번호", "SPOOL_NO", "JOINT_NO", "누계 진행률(%)", "당일 진행률(%)",
               "용접시작일자", "용접시작시간", "일시정지일자", "일시정지시간", "용접완료일자", "용접완료시간",
               "소요시간(분)", "JOINT 누적(분)"]
    export = shown[columns]
    st.dataframe(
        export,
        hide_index=True,
        width="stretch",
        column_config={
            "누계 진행률(%)": st.column_config.NumberColumn(format="%d%%"),
            "당일 진행률(%)": st.column_config.NumberColumn(format="%d%%"),
        },
    )

    # Downloads are independent of the table filters: one scope for the past three
    # Korean calendar days (including today), and one for the full history.
    today = now().date()
    recent_start = today - timedelta(days=2)
    recent_export = df.loc[start.dt.date.between(recent_start, today), columns]
    all_export = df[columns]

    # ASCII file names and UTF-8 BOM: Korean file names / CSV encodings can break on phones.
    stamp = now().strftime("%Y%m%d_%H%M")
    st.subheader("다운로드")
    st.caption(
        f"최근 3일: {recent_start:%Y-%m-%d} ~ {today:%Y-%m-%d} · 용접시작일 기준 · 표 필터와 관계없이 다운로드"
    )
    with st.container(horizontal=True):
        st.download_button(
            "📥 최근 3일 Excel",
            to_excel(recent_export),
            file_name=f"welding_last_3_days_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            width="stretch",
        )
        st.download_button(
            "📥 최근 3일 CSV",
            recent_export.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"welding_last_3_days_{stamp}.csv",
            mime="text/csv",
            width="stretch",
        )

    with st.container(horizontal=True):
        st.download_button(
            "📥 전체 데이터 Excel",
            to_excel(all_export),
            file_name=f"welding_all_data_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
        st.download_button(
            "📥 전체 데이터 CSV",
            all_export.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"welding_all_data_{stamp}.csv",
            mime="text/csv",
            width="stretch",
        )
    st.caption("휴대폰에서는 Excel 파일을 권장합니다. (한글이 깨지지 않음)")


# ---------------------------------------------------------------- app ----
PAGES = {
    "start": st.Page(start_page, title="용접시작", icon="🔥", default=True),  # served at the root URL
    "finish": st.Page(finish_page, title="용접완료", icon="✅", url_path="finish"),
    "list": st.Page(list_page, title="용접목록", icon="📋", url_path="list"),
}
st.navigation(list(PAGES.values()), position="hidden").run()
