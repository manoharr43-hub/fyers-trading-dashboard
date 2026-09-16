import base64
import os
import requests
import streamlit as st
from datetime import datetime


# ============================================================
# AI CHART ANALYSIS
# FYERS / SCANNER ARE NOT USED HERE
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
# SECRET HELPER
# ============================================================

def _secret(name, default=""):
    try:
        value = st.secrets.get(name, None)
        if value:
            return value
    except Exception:
        pass

    return os.getenv(name, default)


# ============================================================
# IMAGE MIME DETECTION
# ============================================================

def _get_mime(image_bytes):
    """
    Detect image MIME type safely.
    """

    if not image_bytes:
        return "image/png"

    # PNG
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"

    # JPEG
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # WEBP
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:20]:
        return "image/webp"

    # Default
    return "image/png"


# ============================================================
# AI RESPONSE EXTRACTION
# ============================================================

def _extract_ai_text(data):
    """
    Extract text from Ollama response.
    """

    try:
        message = data.get("message", {})

        if isinstance(message, dict):
            content = message.get("content", "")

            if content:
                return str(content).strip()

    except Exception:
        pass

    # fallback
    try:
        response = data.get("response", "")

        if response:
            return str(response).strip()
    except Exception:
        pass

    return ""


# ============================================================
# CHART ANALYSIS
# ============================================================

def _analyze_chart(image_bytes, mime=None):

    if not image_bytes:
        return "❌ No chart image received."

    if mime is None:
        mime = _get_mime(image_bytes)

    # --------------------------------------------------------
    # Convert image to Base64
    # --------------------------------------------------------

    image_base64 = base64.b64encode(image_bytes).decode("utf-8")

    # --------------------------------------------------------
    # System Prompt
    # --------------------------------------------------------

    system_prompt = """
You are an expert technical chart-analysis assistant.

IMPORTANT RULES:

1. Analyze ONLY the chart image supplied by the user.
2. DO NOT use FYERS live data.
3. DO NOT use NSE/F&O Scanner data.
4. DO NOT assume live market data.
5. DO NOT invent prices.
6. If a price/value cannot be clearly read, write N/A.
7. If timeframe cannot be identified, write N/A.
8. If instrument cannot be identified, write N/A.
9. Separate visible chart facts from inference.
10. Never claim certainty.
11. Never guarantee profit.
12. Do not say that a movement WILL happen.
13. Use probability/conditional language.
14. If the chart is unclear, say WAIT / insufficient information.
15. Focus on technical chart structure.
16. Detect early conditions that may precede a movement,
    but clearly state that confirmation is required.
17. Analyze candles, trend, structure, support, resistance,
    breakout, breakdown, liquidity, momentum and volume
    only when visible.
"""


    # --------------------------------------------------------
    # User Prompt
    # --------------------------------------------------------

    user_prompt = """
Analyze this chart screenshot.

Return the analysis in the following exact structure:

==================================================
🤖 AI CHART ANALYSIS
==================================================

1. Instrument:
2. Timeframe:
3. Current Price:
4. Visible Market Direction:

--------------------------------------------------
📈 TREND & MARKET STRUCTURE
--------------------------------------------------

5. Trend:
6. Higher High / Higher Low:
7. Lower High / Lower Low:
8. Swing High:
9. Swing Low:

--------------------------------------------------
🎯 KEY LEVELS
--------------------------------------------------

10. Support 1:
11. Support 2:
12. Resistance 1:
13. Resistance 2:

--------------------------------------------------
🚀 BREAKOUT / BREAKDOWN
--------------------------------------------------

14. Breakout Condition:
15. Breakdown Condition:
16. Fake Breakout Possibility:

--------------------------------------------------
💧 LIQUIDITY
--------------------------------------------------

17. Liquidity Zone:
18. Possible Stop Hunt / Liquidity Sweep:

--------------------------------------------------
⚡ MOMENTUM
--------------------------------------------------

19. Momentum:
20. Candle Pattern:
21. Volume Condition:

--------------------------------------------------
🔮 EARLY MOVEMENT DETECTION
--------------------------------------------------

22. Early Bullish Condition:
23. Early Bearish Condition:
24. Confirmation Needed:
25. Next Probable Move:

--------------------------------------------------
📊 OPTIONS BIAS
--------------------------------------------------

26. CALL / PUT Bias:
27. Bias Reason:

--------------------------------------------------
💰 TRADE SETUP
--------------------------------------------------

28. Entry Zone:
29. Stop Loss:
30. Target 1:
31. Target 2:
32. Risk / Reward:

--------------------------------------------------
⏱ SCALPING
--------------------------------------------------

33. Scalping Setup:
34. Holding Time:

--------------------------------------------------
⚠️ RISK
--------------------------------------------------

35. Warning Signals:
36. Invalid Condition:

--------------------------------------------------
🧠 FINAL
--------------------------------------------------

37. Confidence:
38. Final Action:

Final Action MUST be one of:

BUY CALL
BUY PUT
WAIT

Do not force a trade.

If the chart does not provide enough information,
use WAIT.

Remember:
This is chart-image analysis only.
Do not use FYERS.
Do not use Scanner.
Do not use external live market data.
"""


    # --------------------------------------------------------
    # Ollama Payload
    # --------------------------------------------------------

    payload = {
        "model": OLLAMA_MODEL,

        "stream": False,

        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_prompt,
                "images": [
                    image_base64
                ]
            }
        ],

        "options": {
            "temperature": 0.1
        }
    }


    # --------------------------------------------------------
    # API Request
    # --------------------------------------------------------

    try:

        response = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=180
        )

    except requests.exceptions.ConnectionError:

        return f"""
❌ AI CONNECTION FAILED

Ollama server is not reachable.

Current Ollama URL:

{OLLAMA_URL}

Make sure Ollama is running and the selected
vision model is available.

Model:

{OLLAMA_MODEL}
"""

    except requests.exceptions.Timeout:

        return """
❌ AI ANALYSIS TIMEOUT

The vision model took too long to respond.

Try:
• Smaller chart screenshot
• Smaller vision model
• Restart Ollama
"""

    except requests.exceptions.RequestException as e:

        return f"""
❌ AI REQUEST ERROR

{str(e)}
"""


    # --------------------------------------------------------
    # HTTP Error
    # --------------------------------------------------------

    if response.status_code >= 400:

        try:
            error_data = response.json()

            error_message = (
                error_data.get("error")
                or error_data.get("message")
                or str(error_data)
            )

        except Exception:
            error_message = response.text

        return f"""
❌ OLLAMA ERROR

HTTP Status: {response.status_code}

{error_message}

Model:
{OLLAMA_MODEL}
"""


    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    try:

        data = response.json()

    except Exception:

        return """
❌ AI returned an invalid response.
"""


    # --------------------------------------------------------
    # Extract AI Text
    # --------------------------------------------------------

    report = _extract_ai_text(data)

    if not report:

        return """
❌ AI returned an empty analysis.

Please try RE-ANALYZE CHART.
"""

    return report


# ============================================================
# UPLOAD IMAGE
# ============================================================

def _get_uploaded_image():

    uploaded = st.file_uploader(
        "📤 Upload Chart Screenshot",
        type=["png", "jpg", "jpeg", "webp"],
        key="ai_chart_upload"
    )

    if uploaded is None:
        return None, None

    try:
        image_bytes = uploaded.getvalue()

        if not image_bytes:
            return None, None

        mime = _get_mime(image_bytes)

        return image_bytes, mime

    except Exception:
        return None, None


# ============================================================
# PASTE IMAGE
# ============================================================

def _get_pasted_image():

    try:

        from streamlit_paste_button import paste_image_button

    except ImportError:

        st.warning(
            "Paste button package missing. "
            "Run: pip install streamlit-paste-button"
        )

        return None, None


    try:

        pasted = paste_image_button(
            "📋 PASTE CHART",
            key="ai_chart_paste"
        )

    except Exception:

        return None, None


    if pasted is None:
        return None, None


    try:

        # Different package versions can return
        # different objects.

        if hasattr(pasted, "image_data"):

            image_data = pasted.image_data

            if image_data:
                return image_data, _get_mime(image_data)


        if isinstance(pasted, bytes):

            return pasted, _get_mime(pasted)


    except Exception:
        pass


    return None, None


# ============================================================
# MAIN PAGE
# ============================================================

def show_ai_chart_analysis(client=None):

    # --------------------------------------------------------
    # IMPORTANT
    # --------------------------------------------------------
    # client may be FYERS client passed by app.py.
    #
    # We intentionally DO NOT use it.
    #
    # This keeps app.py compatible while making AI analysis
    # independent from FYERS.
    # --------------------------------------------------------

    st.markdown(
        """
        <h1 style="text-align:center;">
            🤖 AI CHART ANALYSIS
        </h1>
        """,
        unsafe_allow_html=True
    )

    st.caption(
        "📊 Chart Screenshot → Vision AI Analysis"
    )


    # ========================================================
    # SESSION STATE
    # ========================================================

    if "ai_chart_report" not in st.session_state:
        st.session_state.ai_chart_report = ""

    if "ai_chart_image" not in st.session_state:
        st.session_state.ai_chart_image = None

    if "ai_chart_mime" not in st.session_state:
        st.session_state.ai_chart_mime = None

    if "analysis_timestamp" not in st.session_state:
        st.session_state.analysis_timestamp = None


    # ========================================================
    # INFORMATION
    # ========================================================

    st.info(
        """
        📌 Upload or paste a TradingView / F&O / NSE / BSE /
        chart screenshot.

        AI will analyze the visible chart only.

        ❌ No FYERS live data  
        ❌ No Scanner data  
        ❌ No external market data
        """
    )


    # ========================================================
    # INPUT COLUMNS
    # ========================================================

    col1, col2 = st.columns(2)


    # --------------------------------------------------------
    # Upload
    # --------------------------------------------------------

    with col1:

        upload_image, upload_mime = _get_uploaded_image()

        if upload_image:

            st.session_state.ai_chart_image = upload_image
            st.session_state.ai_chart_mime = upload_mime


    # --------------------------------------------------------
    # Paste
    # --------------------------------------------------------

    with col2:

        paste_image, paste_mime = _get_pasted_image()

        if paste_image:

            st.session_state.ai_chart_image = paste_image
            st.session_state.ai_chart_mime = paste_mime


    # ========================================================
    # CURRENT IMAGE
    # ========================================================

    image_bytes = st.session_state.ai_chart_image
    image_mime = st.session_state.ai_chart_mime


    if image_bytes:

        st.markdown(
            "### 🖼️ CHART PREVIEW"
        )

        st.image(
            image_bytes,
            caption="Preview — this exact image will be submitted",
            use_container_width=True
        )


        # ----------------------------------------------------
        # Image Info
        # ----------------------------------------------------

        size_kb = len(image_bytes) / 1024

        info_col1, info_col2, info_col3 = st.columns(3)

        with info_col1:
            st.metric(
                "Image Size",
                f"{size_kb:.1f} KB"
            )

        with info_col2:
            st.metric(
                "MIME",
                image_mime or "unknown"
            )

        with info_col3:
            st.metric(
                "AI Model",
                OLLAMA_MODEL
            )


    else:

        st.warning(
            "📷 Please upload or paste a chart screenshot."
        )


    # ========================================================
    # BUTTONS
    # ========================================================

    col_a, col_b, col_c = st.columns(
        [2, 1, 1]
    )


    # --------------------------------------------------------
    # SUBMIT
    # --------------------------------------------------------

    with col_a:

        submit = st.button(
            "🧠 SUBMIT CHART → ANALYZE",
            use_container_width=True,
            type="primary"
        )


    # --------------------------------------------------------
    # RE-ANALYZE
    # --------------------------------------------------------

    with col_b:

        reanalyze = st.button(
            "🔄 RE-ANALYZE",
            use_container_width=True
        )


    # --------------------------------------------------------
    # CLEAR
    # --------------------------------------------------------

    with col_c:

        clear = st.button(
            "🗑️ CLEAR",
            use_container_width=True
        )


    # ========================================================
    # CLEAR
    # ========================================================

    if clear:

        st.session_state.ai_chart_report = ""
        st.session_state.ai_chart_image = None
        st.session_state.ai_chart_mime = None
        st.session_state.analysis_timestamp = None

        st.rerun()


    # ========================================================
    # ANALYZE
    # ========================================================

    if (submit or reanalyze):

        if not image_bytes:

            st.error(
                "❌ First upload or paste a chart."
            )

        else:

            with st.spinner(
                "🤖 AI is analyzing the chart..."
            ):

                report = _analyze_chart(
                    image_bytes,
                    image_mime
                )


            st.session_state.ai_chart_report = report

            st.session_state.analysis_timestamp = (
                datetime.now().strftime(
                    "%d-%m-%Y %H:%M:%S"
                )
            )


    # ========================================================
    # REPORT
    # ========================================================

    report = st.session_state.ai_chart_report


    if report:

        st.markdown(
            "---"
        )

        st.markdown(
            "## 📊 AI ANALYSIS REPORT"
        )


        if st.session_state.analysis_timestamp:

            st.caption(
                f"🕒 Analyzed: "
                f"{st.session_state.analysis_timestamp}"
            )


        # ----------------------------------------------------
        # Report
        # ----------------------------------------------------

        st.markdown(
            report
        )


        # ----------------------------------------------------
        # Download
        # ----------------------------------------------------

        st.download_button(
            label="⬇️ DOWNLOAD ANALYSIS",
            data=report,
            file_name="ai_chart_analysis.txt",
            mime="text/plain",
            use_container_width=True
        )


        # ----------------------------------------------------
        # Disclaimer
        # ----------------------------------------------------

        st.warning(
            """
            ⚠️ IMPORTANT

            This analysis is based only on the supplied chart
            screenshot.

            It is not a guarantee of future price movement.

            Entry, stop-loss and targets are technical
            analysis outputs and should be independently
            verified before any trading decision.
            """
        )


# ============================================================
# SIDEBAR
# ============================================================

def show_sidebar():

    with st.sidebar:

        st.markdown(
            "## 🤖 AI CHART ANALYSIS"
        )

        st.markdown("---")


        st.markdown(
            """
            ### 📌 How it works

            1. Upload / Paste chart
            2. Submit Chart
            3. Vision AI reads the chart
            4. Technical structure is generated

            ### 🔒 Data source

            Chart image only.

            FYERS live data is NOT used.

            NSE/F&O Scanner is NOT used.
            """
        )


        st.markdown("---")


        st.markdown(
            """
            ### ⚠️ Risk

            AI analysis can be wrong.

            Market conditions can change rapidly.

            Always verify the chart before trading.
            """
        )


# ============================================================
# STANDALONE MODE
# ============================================================

if __name__ == "__main__":

    show_sidebar()

    show_ai_chart_analysis()
