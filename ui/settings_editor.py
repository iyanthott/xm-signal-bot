"""Local settings editor for the trading signal bot (Streamlit).

Edits config/settings.yaml only. Never places trades, never talks to Telegram.
Every save is validated with the bot's own loader and backed up first; comments in the
YAML file are preserved. The running bot reads settings at startup, so restart it after saving.

Run (from the project root):
    python -m poetry run streamlit run ui/settings_editor.py
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_signal_bot.settings import load_yaml_config  # noqa: E402

CONFIG_PATH = ROOT / "config" / "settings.yaml"
BACKUP_DIR = ROOT / "config" / "backups"
ENV_PATH = ROOT / ".env"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_yaml = YAML()  # round-trip: keeps comments and key order
_yaml.preserve_quotes = True
_yaml.width = 120
_yaml.indent(mapping=2, sequence=4, offset=2)


# --------------------------------------------------------------------------- helpers
def load_doc():
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return _yaml.load(fh)


def validate_and_save(doc) -> tuple[bool, str]:
    """Validate with the bot's loader (temp file), back up the old file, then write."""
    tmp = CONFIG_PATH.with_name(".settings.candidate.yaml")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            _yaml.dump(doc, fh)
        load_yaml_config(tmp)  # raises ValueError/etc. on any invalid value
    except Exception as exc:  # noqa: BLE001 - show the bot's own message to the user
        return False, f"Not saved. {exc}"
    finally:
        tmp.unlink(missing_ok=True)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CONFIG_PATH, BACKUP_DIR / f"settings-{stamp}.yaml")
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        _yaml.dump(doc, fh)
    return True, f"Saved. Backup: config/backups/settings-{stamp}.yaml. Restart the bot to apply."


def check_symbols_on_mt5(names: list[str]) -> dict[str, bool]:
    """Read-only: ask the running MT5 terminal whether each broker symbol exists."""
    import MetaTrader5 as mt5  # lazy: page still works without the terminal
    from dotenv import dotenv_values

    env = dotenv_values(ENV_PATH)
    ok = mt5.initialize(
        path=env.get("MT5_TERMINAL_PATH") or None,
        login=int(env["MT5_LOGIN"]),
        password=env["MT5_PASSWORD"],
        server=env["MT5_SERVER"],
    )
    if not ok:
        raise RuntimeError(f"MT5 not reachable: {mt5.last_error()}")
    try:
        return {n: mt5.symbol_info(n) is not None for n in names}
    finally:
        mt5.shutdown()


# --------------------------------------------------------------------------- page
st.set_page_config(page_title="Signal Bot Settings", page_icon="📊", layout="wide")
st.title("📊 Signal Bot Settings")
st.caption(
    "Edits `config/settings.yaml`. Alert-only bot: nothing here can place trades. "
    "Changes apply after you restart the bot."
)

if "doc" not in st.session_state:
    st.session_state.doc = load_doc()
doc = st.session_state.doc

tab_mk, tab_ind, tab_flt, tab_risk, tab_strat, tab_bak = st.tabs(
    ["Markets", "Indicators", "Filters", "Stop-loss / Take-profit", "Strategy & alerts", "Backups"]
)

# ---- Markets
with tab_mk:
    st.subheader("Markets")
    st.write("**Label** is what appears in alerts. **MT5 symbol** is XM's exact name (e.g. gold is `GOLD`).")
    rows = [{"Label": k, "MT5 symbol": str(v)} for k, v in doc["symbols"].items()]
    edited = st.data_editor(rows, num_rows="dynamic", width="stretch", key="symbols_ed")
    if st.button("Check these symbols on MT5 (read-only)"):
        try:
            res = check_symbols_on_mt5([r["MT5 symbol"] for r in edited if r.get("MT5 symbol")])
            for sym, found in res.items():
                (st.success if found else st.error)(f"{sym}: {'found' if found else 'NOT found on this broker'}")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
    st.info(f"Timeframes are fixed by the strategy: **{doc['timeframes']['primary']}** trigger, "
            f"**{doc['timeframes']['confirmation']}** confirmation.")

# ---- Indicators
with tab_ind:
    st.subheader("LWMA (trend)")
    c1, c2 = st.columns(2)
    lwma_fast = c1.number_input("Fast period", 1, 5000, int(doc["indicators"]["lwma"]["fast"]))
    lwma_slow = c2.number_input("Slow period", 1, 5000, int(doc["indicators"]["lwma"]["slow"]))
    st.subheader("Stochastic")
    c1, c2, c3 = st.columns(3)
    st_k = c1.number_input("%K period", 1, 500, int(doc["indicators"]["stochastic"]["k"]))
    st_d = c2.number_input("%D period", 1, 500, int(doc["indicators"]["stochastic"]["d"]))
    st_sl = c3.number_input("Slowing", 1, 500, int(doc["indicators"]["stochastic"]["slowing"]))
    bz = doc["indicators"]["stochastic"]["buy_zone"]
    sz = doc["indicators"]["stochastic"]["sell_zone"]
    c1, c2 = st.columns(2)
    buy_zone = c1.slider("BUY zone (%K must be inside)", 0, 100, (int(bz[0]), int(bz[1])))
    sell_zone = c2.slider("SELL zone (%K must be inside)", 0, 100, (int(sz[0]), int(sz[1])))
    st.caption("Fast LWMA must be below slow, and the BUY zone must sit below the SELL zone. "
               "Invalid combinations are rejected on save.")

# ---- Filters
with tab_flt:
    sess = doc["session_filter"]
    st.subheader("Trading session")
    sess_on = st.toggle("Only alert inside these hours", bool(sess["enabled"]))
    st.write(f"Timezone: `{sess['timezone']}` (24-hour HH:MM)")
    win_rows = [{"Start": str(w["start"]), "End": str(w["end"])} for w in sess["windows"]]
    wins = st.data_editor(win_rows, num_rows="dynamic", width="stretch", key="windows_ed")
    st.subheader("Market regime (ADX)")
    reg = doc["regime_filter"]
    reg_on = st.toggle("Skip signals when the market is not trending", bool(reg["enabled"]))
    c1, c2 = st.columns(2)
    adx_p = c1.number_input("ADX period", 2, 200, int(reg["adx_period"]))
    adx_min = c2.number_input("Minimum ADX", 0.0, 100.0, float(reg["min_adx"]), step=0.5)

# ---- Risk
with tab_risk:
    st.subheader("Risk context shown in each alert (information only)")
    rc = doc["risk_context"]
    rc_on = st.toggle("Include stop distance, invalidation and TP levels", bool(rc["enabled"]))
    c1, c2 = st.columns(2)
    atr_p = c1.number_input("ATR period", 1, 200, int(rc["atr_period"]))
    atr_m = c2.number_input("Stop distance = ATR ×", 0.1, 20.0, float(rc["atr_stop_multiplier"]), step=0.1)
    c1, c2 = st.columns(2)
    tp1 = c1.number_input("TP1 risk:reward", 0.1, 20.0, float(rc["rr_targets"][0]), step=0.1)
    tp2 = c2.number_input("TP2 risk:reward", 0.1, 20.0, float(rc["rr_targets"][1]), step=0.1)

# ---- Strategy & alerts
with tab_strat:
    st.subheader("Signal types")
    strat = doc["strategy"]
    chain_on = st.toggle("Chained signals (M15 trigger → M1 confirmations)", bool(strat["chain"]["enabled"]))
    legacy_on = st.toggle("Legacy scenarios", bool(strat["enable_legacy_scenarios"]))
    summary_on = st.toggle("Summary alert when several scenarios match", bool(strat["summary"]["enabled"]))
    st.subheader("Duplicate prevention")
    cooldown = st.number_input(
        "Cooldown per symbol + direction (minutes)", 0, 1440, int(doc["signal_dedup"]["cooldown_minutes"])
    )
    st.subheader("Bot health")
    hb_on = st.toggle("Heartbeat file", bool(doc["monitoring"]["heartbeat_enabled"]))
    health_on = st.toggle("Health alerts to Telegram (bot started, symbol problems)", bool(doc["health_alerts"]["enabled"]))
    st.divider()
    st.markdown(
        "**Your own indicator or rules?** This page covers the existing strategy's numbers. "
        "A brand-new indicator or different BUY/SELL logic needs code in `src/trading_signal_bot/indicators/` "
        "and `strategy.py`."
    )

# ---- Backups
with tab_bak:
    st.subheader("Restore a previous version")
    backups = sorted(BACKUP_DIR.glob("settings-*.yaml"), reverse=True) if BACKUP_DIR.exists() else []
    if not backups:
        st.write("No backups yet. One is created every time you save.")
    else:
        pick = st.selectbox("Backup", [b.name for b in backups])
        if st.button("Restore selected backup"):
            shutil.copy2(BACKUP_DIR / pick, CONFIG_PATH)
            st.session_state.pop("doc", None)
            st.success("Restored. Restart the bot to apply.")
            st.rerun()

# ---- Save bar
st.divider()
if st.button("💾 Save settings", type="primary"):
    problems = []
    syms = [r for r in edited if r.get("Label") and r.get("MT5 symbol")]
    if not syms:
        problems.append("Add at least one market.")
    labels = [r["Label"].strip() for r in syms]
    if len(set(labels)) != len(labels):
        problems.append("Market labels must be unique.")
    win_ok = [w for w in wins if w.get("Start") or w.get("End")]
    if sess_on and not win_ok:
        problems.append("Session filter is on but no hours are set.")
    for w in win_ok:
        if not (HHMM.match(str(w.get("Start", ""))) and HHMM.match(str(w.get("End", "")))):
            problems.append(f"Session hours must be HH:MM, got {w.get('Start')}–{w.get('End')}.")
    if problems:
        for p in problems:
            st.error(p)
    else:
        new = load_doc()
        sym = new["symbols"]
        for key in list(sym.keys()):
            del sym[key]
        for r in syms:
            sym[r["Label"].strip()] = DoubleQuotedScalarString(r["MT5 symbol"].strip())
        ind = new["indicators"]
        ind["lwma"]["fast"], ind["lwma"]["slow"] = int(lwma_fast), int(lwma_slow)
        s = ind["stochastic"]
        s["k"], s["d"], s["slowing"] = int(st_k), int(st_d), int(st_sl)
        s["buy_zone"][0], s["buy_zone"][1] = buy_zone
        s["sell_zone"][0], s["sell_zone"][1] = sell_zone
        sf = new["session_filter"]
        sf["enabled"] = bool(sess_on)
        for i, w in enumerate(win_ok):
            if i < len(sf["windows"]):
                sf["windows"][i]["start"], sf["windows"][i]["end"] = str(w["Start"]), str(w["End"])
            else:
                sf["windows"].append({"start": str(w["Start"]), "end": str(w["End"])})
        del sf["windows"][len(win_ok):]
        rf = new["regime_filter"]
        rf["enabled"], rf["adx_period"], rf["min_adx"] = bool(reg_on), int(adx_p), float(adx_min)
        r_ = new["risk_context"]
        r_["enabled"], r_["atr_period"], r_["atr_stop_multiplier"] = bool(rc_on), int(atr_p), float(atr_m)
        r_["rr_targets"][0], r_["rr_targets"][1] = float(tp1), float(tp2)
        new["strategy"]["chain"]["enabled"] = bool(chain_on)
        new["strategy"]["enable_legacy_scenarios"] = bool(legacy_on)
        new["strategy"]["summary"]["enabled"] = bool(summary_on)
        new["signal_dedup"]["cooldown_minutes"] = int(cooldown)
        new["monitoring"]["heartbeat_enabled"] = bool(hb_on)
        new["health_alerts"]["enabled"] = bool(health_on)
        ok, msg = validate_and_save(new)
        (st.success if ok else st.error)(msg)
        if ok:
            st.session_state.doc = load_doc()
