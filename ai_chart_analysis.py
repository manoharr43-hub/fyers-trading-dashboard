import base64
import requests
import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
import plotly.graph_objects as go
from streamlit_autorefresh import rerun_script
import os
from dotenv import load_dotenv


# ============================================================
# LOAD ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="🤖 AI Chart Analysis",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ============================================================
# CUSTOM STYLING
# ============================================================

st.markdown("""
<style>
    .main {
        padding: 2rem;
    }
    .stButton>button {
        width: 100%;
        border-radius: 5px;
        font-weight: 600;
    }
    .header-text {
        color: #1f77b4;
        font-weight: 700;
    }
</style>
""", unsafe_allow_html=True)


# ============================================================
# OPENAI CONFIG
# ============================================================

OPENAI_API_URL = "https://api.openai.com/v1/messages"


# ============================================================
# STREAMLIT SECRETS HELPER
# ============================================================

def _secret(name, default=""):
    """Safely retrieve secrets from Streamlit config or environment."""
    try:
        # Try Streamlit secrets first
        value = st.secrets.get(name, None)
        if value:
            return str(value).strip()
    except Exception:
        pass
    
    # Try environment variables
    value = os.getenv(name, default)
    if value:
        return str(value).strip()
    
    return default


# ============================================================
# IMAGE MIME DETECTION
# ============================================================

def _chart_image_mime(image_bytes):
    """
    Detect image MIME type from actual file bytes.
    Supports: PNG, JPEG, WebP
    """
    if not image_bytes:
        return "image/png"

    # PNG signature
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # JPEG signature
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # WebP signature
    if (
        image_bytes.startswith(b"RIFF")
        and len(image_bytes) >= 12
        and image_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"

    # Default fallback
    return "image/png"


# ============================================================
# OUTPUT TEXT EXTRACTOR
# ============================================================

def _output_text(data):
    """
    Extract text from OpenAI API response.
    Handles both new and legacy response formats.
    """
    # New API response format
    if isinstance(data, dict) and data.get("content"):
        parts = []
        for content_block in data.get("content", []):
            if isinstance(content_block, dict) and content_block.get("type") == "text":
                text = content_block.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    
    # Legacy format
    if data.get("output_text"):
        return str(data["output_text"]).strip()

    parts = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue

        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue

            if content.get("type") == "text":
                text = content.get("text")
                if text:
                    parts.append(str(text))

    return "\n".join(parts).strip()


# ============================================================
# OPENAI CHART ANALYSIS
# ============================================================

def _analyze(image_bytes, mime=None):
    """
    Submit chart image to OpenAI for technical analysis.
    Returns detailed chart analysis report.
    """

    if not image_bytes:
        raise RuntimeError("❌ No chart image was supplied.")

    # --------------------------------------------------------
    # API KEY VALIDATION
    # --------------------------------------------------------

    key = _secret("OPENAI_API_KEY")

    if not key:
        raise RuntimeError(
            "❌ OPENAI_API_KEY is missing. "
            "Add OPENAI_API_KEY to environment or Streamlit Secrets."
        )

    # --------------------------------------------------------
    # MODEL SELECTION
    # --------------------------------------------------------

    model = _secret("OPENAI_MODEL", "gpt-4-vision")

    # --------------------------------------------------------
    # MIME TYPE DETECTION
    # --------------------------------------------------------

    detected_mime = _chart_image_mime(image_bytes)
    mime = detected_mime if (not mime or not str(mime).startswith("image/")) else mime
    mime = detected_mime  # Always trust actual file bytes

    # --------------------------------------------------------
    # BASE64 ENCODING
    # --------------------------------------------------------

    image64 = base64.b64encode(image_bytes).decode("utf-8")

    # --------------------------------------------------------
    # TECHNICAL ANALYSIS PROMPT
    # --------------------------------------------------------

    system_prompt = """You are an expert technical chart analyst with 20+ years of trading experience in NSE, BSE, F&O, and international markets.

CRITICAL RULES:
1. Analyze ONLY the supplied chart image - no external data
2. Do NOT assume instrument if not visible
3. Do NOT invent prices, levels, or candle values
4. If information is unclear or unreadable, write "N/A"
5. Separate visible FACTS from INFERENCE clearly
6. Never claim certainty about future price movement
7. Give practical analysis based only on visible information
8. If chart is too unclear, recommend NOT to trade"""

    analysis_prompt = """Analyze this trading chart and provide a comprehensive technical analysis report:

1. INSTRUMENT
   - What is being traded? (Stock, Index, Commodity, etc.)

2. TIMEFRAME
   - What is the chart timeframe? (1min, 5min, 15min, 1H, 4H, 1D, etc.)

3. CURRENT PRICE
   - What is the latest visible price?
   - Is this a high/low/close price?

4. MARKET DIRECTION
   - BULLISH (uptrend)
   - BEARISH (downtrend)
   - SIDEWAYS (range/consolidation)

5. TREND STRUCTURE
   - Higher High / Higher Low (uptrend)
   - Lower High / Lower Low (downtrend)
   - Range/Consolidation
   - Trend change if visible

6. SUPPORT LEVELS
   - Key support price levels (use N/A if unreadable)

7. RESISTANCE LEVELS
   - Key resistance price levels (use N/A if unreadable)

8. BREAKOUT/BREAKDOWN STATUS
   - Confirmed breakout (with volume)
   - Confirmed breakdown (with volume)
   - Possible breakout/breakdown
   - None visible

9. LIQUIDITY PATTERNS
   - Liquidity sweeps visible?
   - Rejection zones?
   - Stop hunts?

10. VOLUME ANALYSIS
    - Is volume confirming price action?
    - High volume breakout/breakdown?

11. MOMENTUM INDICATORS
    - RSI, MACD, Stochastic, or other visible indicators
    - What does momentum show?

12. CALL/PUT BIAS (For F&O traders)
    - Should I buy CALLS or PUTS?
    - Why? (Technical reasoning)

13. SCALPING SETUP
    - Is this a good scalp setup?
    - If yes, how many points target?
    - If no, why not?

14. ENTRY POINT
    - EXACT entry price (only if clearly derivable)
    - Or write "N/A" if unclear

15. STOP LOSS
    - Where should SL be placed?
    - Points above/below current price?

16. TARGET 1 (First profit taking)
    - Target price or points?

17. TARGET 2 (Second profit taking)
    - Target price or points?

18. RISK/REWARD RATIO
    - Calculate: (Target - Entry) / (Entry - SL)
    - Should be minimum 1:2

19. HOLDING TIME
    - How long should this trade be held?
    - Scalp (seconds to minutes)
    - Swing (hours to days)
    - Position (days to weeks)

20. NEXT LIKELY MOVE
    - What's the next expected price action?
    - Probability-based reasoning (not certainty)

21. CONFLUENCE
    - How many technical indicators confirm this setup?
    - Is there confluence with multiple timeframes?

22. CONFIDENCE LEVEL
    - LOW (unclear setup, conflicting signals)
    - MEDIUM (some confirmation, moderate risk)
    - HIGH (strong confluence, clear signals)

23. WARNING SIGNALS
    - Any signals suggesting to AVOID this trade?
    - Divergences? Weak volume? Unconfirmed breakout?

24. FINAL ACTION
    Choose EXACTLY ONE:
    ✅ BUY CALL (for upside)
    ✅ BUY PUT (for downside)
    ✅ SELL CALL (for downside, advanced)
    ✅ SELL PUT (for upside, advanced)
    ❌ WAIT (if setup is unclear or risky)

CRITICAL REMINDERS:
- Never hallucinate exact price levels
- If anything is unreadable, use "N/A"
- If setup is unclear, FINAL ACTION must be WAIT
- This is technical analysis only - NOT financial advice
- Always verify before trading
- Risk management is critical"""

    # --------------------------------------------------------
    # OPENAI API PAYLOAD
    # --------------------------------------------------------

    payload = {
        "model": model,
        "max_tokens": 3000,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": analysis_prompt
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": image64
                        }
                    }
                ]
            }
        ]
    }

    # --------------------------------------------------------
    # API REQUEST
    # --------------------------------------------------------

    try:
        response = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=120
        )

    except requests.exceptions.Timeout:
        raise RuntimeError("⏱️ OpenAI request timed out. Please try again.")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("🌐 Could not connect to OpenAI API. Check internet connection.")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"🔗 Connection error: {str(e)[:200]}")

    # --------------------------------------------------------
    # API ERROR HANDLING
    # --------------------------------------------------------

    if response.status_code >= 400:
        try:
            error_data = response.json()
            error_message = error_data.get("error", {}).get("message", "")
            if error_message:
                raise RuntimeError(f"OpenAI API Error {response.status_code}: {error_message}")
        except ValueError:
            pass
        raise RuntimeError(f"OpenAI API Error {response.status_code}: {response.text[:500]}")

    # --------------------------------------------------------
    # PARSE JSON RESPONSE
    # --------------------------------------------------------

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("OpenAI returned invalid JSON response.")

    # --------------------------------------------------------
    # EXTRACT ANALYSIS REPORT
    # --------------------------------------------------------

    report = _output_text(data)

    if not report:
        raise RuntimeError("AI returned empty analysis report.")

    return report


# ============================================================
# IMAGE UPLOAD HANDLER
# ============================================================

def _get_uploaded_image():
    """Handle chart image upload."""
    uploaded = st.file_uploader(
        "📷 Upload chart screenshot",
        type=["png", "jpg", "jpeg", "webp"],
        key="ai_chart_upload_main"
    )

    if uploaded is not None:
        try:
            image_bytes = uploaded.getvalue()
            if image_bytes:
                mime = _chart_image_mime(image_bytes)
                return image_bytes, mime
        except Exception as e:
            st.error(f"❌ Error reading image: {e}")

    return None, None


# ============================================================
# CLIPBOARD PASTE HANDLER
# ============================================================

def _get_pasted_image():
    """Handle chart image paste from clipboard."""
    try:
        from streamlit_paste_button import paste_image_button

        pasted = paste_image_button(
            label="📋 Paste chart from clipboard",
            key="ai_chart_paste_main"
        )

        if pasted:
            if isinstance(pasted, dict):
                image_bytes = pasted.get("bytes") or pasted.get("image")
                if image_bytes:
                    detected = _chart_image_mime(image_bytes)
                    return image_bytes, detected

    except ImportError:
        st.caption("💡 Clipboard paste plugin not installed.")
    except Exception as e:
        st.warning(f"⚠️ Clipboard error: {e}")

    return None, None


# ============================================================
# MAIN APPLICATION
# ============================================================

def show_ai_chart_analysis():
    """Main chart analysis interface."""

    # ========================================================
    # HEADER
    # ========================================================

    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("# 🤖 AI CHART ANALYSIS")
    with col2:
        st.caption(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    st.markdown("---")

    st.info(
        "📊 **Upload your trading chart** (TradingView, MT5, Fyers, etc.) for AI-powered technical analysis. "
        "NSE, BSE, F&O, Crypto - all supported!"
    )

    # ========================================================
    # INITIALIZE SESSION STATE
    # ========================================================

    if "ai_chart_report" not in st.session_state:
        st.session_state["ai_chart_report"] = ""

    if "ai_chart_image" not in st.session_state:
        st.session_state["ai_chart_image"] = None

    if "ai_chart_mime" not in st.session_state:
        st.session_state["ai_chart_mime"] = None

    if "analysis_timestamp" not in st.session_state:
        st.session_state["analysis_timestamp"] = None

    # ========================================================
    # IMAGE INPUT SECTION
    # ========================================================

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### 📤 Upload Image")
        uploaded_bytes, uploaded_mime = _get_uploaded_image()

        if uploaded_bytes:
            st.session_state["ai_chart_image"] = uploaded_bytes
            st.session_state["ai_chart_mime"] = uploaded_mime
            st.success("✅ Chart uploaded!")

    with col2:
        st.markdown("### 📋 Paste Image")
        pasted_bytes, pasted_mime = _get_pasted_image()

        if pasted_bytes:
            st.session_state["ai_chart_image"] = pasted_bytes
            st.session_state["ai_chart_mime"] = pasted_mime
            st.success("✅ Chart pasted!")

    # ========================================================
    # GET CURRENT IMAGE
    # ========================================================

    image_bytes = st.session_state.get("ai_chart_image")
    mime = st.session_state.get("ai_chart_mime")

    # ========================================================
    # IMAGE PREVIEW
    # ========================================================

    if image_bytes:
        st.markdown("---")
        st.markdown("### 👁️ Chart Preview")
        col1, col2 = st.columns([2, 1])
        
        with col1:
            st.image(
                image_bytes,
                caption="This exact image will be submitted",
                use_container_width=True
            )
        
        with col2:
            # Show file info
            file_size = len(image_bytes) / 1024  # KB
            st.metric("File Size", f"{file_size:.1f} KB")
            st.metric("MIME Type", mime)

    # ========================================================
    # ANALYSIS CONTROLS
    # ========================================================

    st.markdown("---")

    col1, col2, col3 = st.columns(3)

    with col1:
        submit = st.button(
            "🧠 ANALYZE CHART",
            type="primary",
            use_container_width=True,
            disabled=not bool(image_bytes),
            key="ai_chart_submit"
        )

    with col2:
        reanalyze = st.button(
            "🔄 RE-ANALYZE",
            use_container_width=True,
            disabled=not bool(image_bytes),
            key="ai_chart_reanalyze"
        )

    with col3:
        clear = st.button(
            "🗑️ CLEAR ALL",
            use_container_width=True,
            disabled=not bool(image_bytes or st.session_state.get("ai_chart_report")),
            key="ai_chart_clear"
        )

    # ========================================================
    # EXECUTE ANALYSIS
    # ========================================================

    if (submit or reanalyze) and image_bytes:
        with st.spinner("🧠 AI analyzing chart... Please wait (30-60 seconds)"):
            try:
                report = _analyze(image_bytes, mime)
                st.session_state["ai_chart_report"] = report
                st.session_state["analysis_timestamp"] = datetime.now()
                st.success("✅ Analysis completed!")

            except Exception as e:
                st.session_state["ai_chart_report"] = ""
                st.error(f"❌ Analysis failed:\n{str(e)}")

    # ========================================================
    # HANDLE CLEAR ACTION
    # ========================================================

    if clear:
        st.session_state["ai_chart_image"] = None
        st.session_state["ai_chart_mime"] = None
        st.session_state["ai_chart_report"] = ""
        st.session_state["analysis_timestamp"] = None
        st.rerun()

    # ========================================================
    # DISPLAY REPORT
    # ========================================================

    report = st.session_state.get("ai_chart_report", "")
    timestamp = st.session_state.get("analysis_timestamp")

    if report:
        st.markdown("---")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown("## 📋 Analysis Report")
        with col2:
            if timestamp:
                st.caption(f"📅 {timestamp.strftime('%H:%M:%S')}")

        # Display report in an expander for better readability
        with st.expander("📖 Full Report (Click to expand)", expanded=True):
            st.markdown(report)

        st.markdown("---")

        # Risk Warning
        st.warning(
            "⚠️ **DISCLAIMER**: This is technical analysis only. "
            "Always verify price, entry, stop-loss, and risk management "
            "before taking ANY trade. Not financial advice. "
            "Trade at your own risk!"
        )

        # Download options
        col1, col2, col3 = st.columns(3)

        with col1:
            st.download_button(
                label="📥 Download as Text",
                data=report,
                file_name=f"chart_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                mime="text/plain"
            )

        with col2:
            # Copy to clipboard
            st.button(
                "📋 Copy Report",
                key="copy_report",
                help="Click to copy full report"
            )

        with col3:
            st.button(
                "🔗 Share Analysis",
                key="share_report",
                help="Share this analysis"
            )


# ============================================================
# SIDEBAR INFO
# ============================================================

def show_sidebar():
    """Sidebar information and settings."""
    with st.sidebar:
        st.markdown("---")
        st.markdown("## 📌 ABOUT")
        st.info(
            "🤖 AI-powered technical chart analysis\n\n"
            "📊 Supports:\n"
            "- NSE (Stocks, Indices)\n"
            "- BSE (Stocks)\n"
            "- F&O (Futures, Options)\n"
            "- Crypto (Bitcoin, Altcoins)\n"
            "- Forex, Commodities\n\n"
            "🔌 Powered by: OpenAI Vision API"
        )

        st.markdown("---")
        st.markdown("## ⚙️ SETTINGS")
        
        auto_refresh = st.checkbox("🔄 Auto-refresh", value=False)
        if auto_refresh:
            refresh_interval = st.slider(
                "Refresh interval (seconds)",
                min_value=5,
                max_value=300,
                value=60,
                step=5
            )
            rerun_script(interval=refresh_interval * 1000)

        st.markdown("---")
        st.markdown("## 📚 HELP")
        
        with st.expander("❓ How to Use"):
            st.markdown("""
            1. **Upload Chart**: Take screenshot of your chart
            2. **Submit**: Click 'ANALYZE CHART'
            3. **Wait**: AI analyzes (30-60 seconds)
            4. **Review**: Check analysis and recommendations
            5. **Trade**: Verify levels before entering
            """)

        with st.expander("⚠️ Risk Disclaimer"):
            st.markdown("""
            - This is technical analysis only
            - Not financial advice
            - Always use stop losses
            - Risk only what you can afford
            - Verify analysis before trading
            - Markets are unpredictable
            """)

        st.markdown("---")
        st.caption("🔐 API Key is secure & never stored")
        st.caption("v1.0.0 | © 2024 AI Chart Analysis")


# ============================================================
# APP ENTRY POINT
# ============================================================

if __name__ == "__main__":
    show_sidebar()
    show_ai_chart_analysis()
