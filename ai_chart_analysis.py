import base64
import os
import requests
import streamlit as st
from datetime import datetime


# ============================================================
# 🤖 AI CHART ANALYSIS
# IMPORTANT:
# - FYERS IS NOT USED HERE
# - SCANNER IS NOT USED HERE
# - ONLY CHART IMAGE IS SENT TO AI
# ============================================================

OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://localhost:11434/api/chat"
)

OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL",
    "qwen2.5vl:7b"
)


# ============================================================
# HELPERS
# ============================================================

def _secret(name, default=""):
    try:
        value = st.secrets.get(name, default)
        if value:
            return value
    except Exception:
        pass

    return os.getenv(name, default)


def _get_mime(file_name):
    if not file_name:
        return "image/png"

    name = file_name.lower()

    if name.endswith(".jpg") or name.endswith(".jpeg"):
        return "image/jpeg"

    if name.endswith(".webp"):
        return "image/webp"

    return "image/png"


def _extract_ai_text(data):
    try:
        return data["message"]["content"]
    except Exception:
        pass

    try:
        return data["response"]
    except Exception:
        pass

    return ""


# ============================================================
# AI PROMPT
# ============================================================

AI_SYSTEM_PROMPT = """
You are an AI chart-analysis assistant.

IMPORTANT RULES:

1. Analyze ONLY the supplied chart image.
2. DO NOT use FYERS data.
3. DO NOT use NSE scanner data.
4. DO NOT request or assume live market data.
5. Do not invent price values.
6. If a value is not readable, write N/A.
7. Clearly separate visible chart facts from interpretation.
8. Do not claim certainty.
9. Do not guarantee profit.
10. If the chart is unclear, say WAIT / INSUFFICIENT DATA.
11. Analyze candles, trend structure, support, resistance,
    volume if visible, momentum, breakout/breakdown,
    liquidity sweep, and price action.
12. Use only information visible in the image.

Return a practical structured analysis.
"""


AI_USER_PROMPT = """
Analyze this chart screenshot.

Give the result in the following format:

1. INSTRUMENT
2. TIMEFRAME
3. CURRENT PRICE
4. MARKET DIRECTION
5. TREND STRUCTURE
6. SWING HIGH
7. SWING LOW
8. SUPPORT LEVELS
9. RESISTANCE LEVELS
10. BREAKOUT STATUS
11. BREAKDOWN STATUS
12. LIQUIDITY / STOP HUNT
13. VOLUME OBSERVATION
14. MOMENTUM
15. CANDLE PATTERN
16. CALL / PUT BIAS
17. SCALPING SETUP
18. ENTRY ZONE
19. STOP LOSS
20. TARGET 1
21. TARGET 2
22. RISK / REWARD
23. NEXT PROBABLE MOVE
24. CONFIRMATION REQUIRED
25. WARNING SIGNALS
26. CONFIDENCE
27. FINAL ACTION

For FINAL ACTION use only one:

BUY CALL
BUY PUT
SELL CALL
SELL PUT
WAIT

If the chart does not provide enough information,
use WAIT.

Do not invent numbers.
"""


# ============================================================
# LOCAL AI ANALYSIS
# ============================================================

def _analyze_chart(image_bytes, mime_type):
    if not image_bytes:
        return "❌ Chart image not found."

    try:
        # Convert image to Base64
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        payload = {
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": AI_SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": AI_USER_PROMPT,
                    "images": [image_b64]
                }
            ]
        }

        response = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=180
        )

        if response.status_code != 200:
            return (
                "❌ AI connection error.\n\n"
                f"HTTP Status: {response.status_code}\n\n"
                f"{response.text[:1000]}"
            )

        data = response.json()

        report = _extract_ai_text(data)

        if not report:
            return "❌ AI returned an empty analysis."

        return report

    except requests.exceptions.ConnectionError:
        return """
❌ LOCAL AI CONNECT AVVALÉDU

Ollama running లేదు.

Terminal లో:

ollama serve

అని run చేసి Streamlit app ని మళ్లీ start చేయండి.
"""

    except requests.exceptions.Timeout:
        return """
❌ AI ANALYSIS TIMEOUT

Chart analysis ఎక్కువ సమయం తీసుకుంది.

కొంచెం చిన్న chart screenshot upload చేసి మళ్లీ Analyze చేయండి.
"""

    except Exception as e:
        return f"""
❌ AI ANALYSIS ERROR

{str(e)}
"""


# ============================================================
# IMAGE UPLOAD
# ============================================================

def _get_uploaded_image():
    uploaded = st.file_uploader(
        "📷 Upload Chart Screenshot",
        type=["png", "jpg", "jpeg", "webp"],
        key="ai_chart_upload"
    )

    if uploaded is None:
        return None, None

    return uploaded.getvalue(), _get_mime(uploaded.name)


# ============================================================
# PASTE IMAGE
# ============================================================

def _get_pasted_image():
    try:
        from streamlit_paste_button import paste_image_button

        pasted = paste_image_button(
            label="📋 Paste Chart",
            key="ai_chart_paste"
        )

        if pasted is not None:
            if hasattr(pasted, "image_data"):
                return pasted.image_data, "image/png"

            if isinstance(pasted, bytes):
                return pasted, "image/png"

    except Exception:
        pass

    return None, None


# ============================================================
# MAIN PAGE
# ============================================================

def show_ai_chart_analysis(client=None):
    """
    IMPORTANT:

    client parameter is accepted only for compatibility
    with existing app.py.

    FYERS client is NOT used.
    Scanner is NOT used.
    """

    st.markdown(
        """
        <div style="
            padding:18px;
            border-radius:15px;
            background:linear-gradient(135deg,#111827,#1f2937);
            margin-bottom:20px;
        ">
            <h1 style="margin:0;">🤖 AI CHART ANALYSIS</h1>
            <p style="margin-top:8px;">
                Chart Image → AI Analysis
            </p>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.info(
        "ℹ️ ఈ module లో FYERS LIVE DATA ఉపయోగించబడదు. "
        "Scanner data కూడా ఉపయోగించబడదు. "
        "మీరు ఇచ్చిన Chart Image మాత్రమే AI analyze చేస్తుంది."
    )

    # --------------------------------------------------------
    # SESSION STATE
    # --------------------------------------------------------

    if "ai_chart_report" not in st.session_state:
        st.session_state.ai_chart_report = ""

    if "ai_chart_image" not in st.session_state:
        st.session_state.ai_chart_image = None

    if "ai_chart_mime" not in st.session_state:
        st.session_state.ai_chart_mime = None

    if "ai_chart_time" not in st.session_state:
        st.session_state.ai_chart_time = None

    # --------------------------------------------------------
    # INPUT
    # --------------------------------------------------------

    col1, col2 = st.columns(2)

    with col1:
        upload_bytes, upload_mime = _get_uploaded_image()

    with col2:
        paste_bytes, paste_mime = _get_pasted_image()

    # --------------------------------------------------------
    # SELECT IMAGE
    # --------------------------------------------------------

    image_bytes = None
    mime_type = None

    if upload_bytes:
        image_bytes = upload_bytes
        mime_type = upload_mime

    elif paste_bytes:
        image_bytes = paste_bytes
        mime_type = paste_mime

    # --------------------------------------------------------
    # PREVIEW
    # --------------------------------------------------------

    if image_bytes:

        st.session_state.ai_chart_image = image_bytes
        st.session_state.ai_chart_mime = mime_type

        st.markdown("### 📊 Chart Preview")

        st.image(
            image_bytes,
            use_container_width=True
        )

        size_kb = len(image_bytes) / 1024

        st.caption(
            f"Image Size: {size_kb:.1f} KB | "
            f"MIME: {mime_type}"
        )

    elif st.session_state.ai_chart_image:

        image_bytes = st.session_state.ai_chart_image
        mime_type = st.session_state.ai_chart_mime

        st.markdown("### 📊 Current Chart")

        st.image(
            image_bytes,
            use_container_width=True
        )

    # --------------------------------------------------------
    # BUTTONS
    # --------------------------------------------------------

    st.markdown("---")

    col1, col2, col3 = st.columns(3)

    with col1:
        analyze = st.button(
            "🧠 ANALYZE CHART",
            use_container_width=True,
            type="primary"
        )

    with col2:
        reanalyze = st.button(
            "🔄 RE-ANALYZE",
            use_container_width=True
        )

    with col3:
        clear = st.button(
            "🗑️ CLEAR",
            use_container_width=True
        )

    # --------------------------------------------------------
    # CLEAR
    # --------------------------------------------------------

    if clear:

        st.session_state.ai_chart_report = ""
        st.session_state.ai_chart_image = None
        st.session_state.ai_chart_mime = None
        st.session_state.ai_chart_time = None

        st.rerun()

    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------

    if analyze or reanalyze:

        if not image_bytes:
            st.warning(
                "⚠️ ముందుగా Chart Screenshot upload/paste చేయండి."
            )

        else:

            with st.spinner(
                "🤖 AI Chart ని analyze చేస్తోంది..."
            ):

                report = _analyze_chart(
                    image_bytes,
                    mime_type
                )

            st.session_state.ai_chart_report = report

            st.session_state.ai_chart_time = (
                datetime.now().strftime(
                    "%d-%m-%Y %H:%M:%S"
                )
            )

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    if st.session_state.ai_chart_report:

        st.markdown("---")

        st.markdown("## 🧠 AI ANALYSIS REPORT")

        if st.session_state.ai_chart_time:
            st.caption(
                "Analysis Time: "
                + st.session_state.ai_chart_time
            )

        st.markdown(
            st.session_state.ai_chart_report
        )

        # ----------------------------------------------------
        # DOWNLOAD REPORT
        # ----------------------------------------------------

        st.download_button(
            label="⬇️ DOWNLOAD AI REPORT",
            data=st.session_state.ai_chart_report,
            file_name="AI_Chart_Analysis.txt",
            mime="text/plain",
            use_container_width=True
        )

    else:

        st.markdown(
            """
            <div style="
                padding:30px;
                text-align:center;
                border:1px dashed #555;
                border-radius:15px;
                margin-top:20px;
            ">
                <h3>📷 Chart Screenshot Upload చేయండి</h3>
                <p>
                    తరువాత <b>🧠 ANALYZE CHART</b> click చేయండి.
                </p>
            </div>
            """,
            unsafe_allow_html=True
        )


# ============================================================
# SIDEBAR
# ============================================================

def show_sidebar():

    with st.sidebar:

        st.markdown("## 🤖 AI Chart Analysis")

        st.success(
            "FYERS: Scanner కోసం active\n\n"
            "AI Chart: Image మాత్రమే"
        )

        st.markdown("---")

        st.markdown(
            """
            ### AI analyzes

            • Candles  
            • Trend  
            • Support  
            • Resistance  
            • Breakout  
            • Breakdown  
            • Liquidity  
            • Volume  
            • Momentum  
            • Call / Put Bias  
            • Entry  
            • Stop Loss  
            • Targets  
            • Risk / Reward  
            • Next Move
            """
        )

        st.markdown("---")

        st.warning(
            "⚠️ AI analysis is not a guarantee "
            "of future market movement."
        )


# ============================================================
# STANDALONE MODE
# ============================================================

if __name__ == "__main__":

    show_sidebar()

    show_ai_chart_analysis()
