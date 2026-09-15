import base64
import requests
import streamlit as st


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="AI Chart Analysis",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ============================================================
# OPENAI CONFIG
# ============================================================

OPENAI_API_URL = "https://api.openai.com/v1/messages"


# ============================================================
# STREAMLIT SECRETS HELPER
# ============================================================

def _secret(name, default=""):
    """Safely retrieve secrets from Streamlit config."""
    try:
        value = st.secrets.get(name, default)
        if value is None:
            return default
        return str(value).strip()
    except Exception:
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
    # New API convenience field
    if data.get("output_text"):
        return str(data["output_text"]).strip()

    # Legacy format parsing
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
        raise RuntimeError("No chart image was supplied.")

    # --------------------------------------------------------
    # API KEY VALIDATION
    # --------------------------------------------------------

    key = _secret("OPENAI_API_KEY")

    if not key:
        raise RuntimeError(
            "❌ OPENAI_API_KEY is missing. "
            "Add OPENAI_API_KEY to Streamlit Secrets in your .streamlit/secrets.toml file."
        )

    # --------------------------------------------------------
    # MODEL SELECTION
    # --------------------------------------------------------

    model = _secret("OPENAI_MODEL", "gpt-4-vision")

    # --------------------------------------------------------
    # MIME TYPE DETECTION
    # --------------------------------------------------------

    detected_mime = _chart_image_mime(image_bytes)

    if not mime or not str(mime).startswith("image/"):
        mime = detected_mime
    else:
        # Always trust actual file bytes
        mime = detected_mime

    # --------------------------------------------------------
    # BASE64 ENCODING
    # --------------------------------------------------------

    image64 = base64.b64encode(image_bytes).decode("utf-8")

    # --------------------------------------------------------
    # TECHNICAL ANALYSIS PROMPT
    # --------------------------------------------------------

    system_prompt = """You are an expert technical chart analyst with 15+ years of trading experience.

CRITICAL RULES:
1. Analyze ONLY the supplied chart image - no external data
2. Do NOT assume instrument if not visible
3. Do NOT invent prices, levels, or candle values
4. If information is unclear or unreadable, write "N/A"
5. Separate visible FACTS from INFERENCE clearly
6. Never claim certainty about future price movement
7. Give practical analysis based only on visible information
8. If chart is too unclear, recommend NOT to trade"""

    analysis_prompt = """Analyze this trading chart and provide a detailed technical analysis report with the following sections:

1. INSTRUMENT - What is being traded?
2. TIMEFRAME - What is the chart timeframe?
3. CURRENT PRICE - What is the latest visible price?
4. MARKET DIRECTION - BULLISH / BEARISH / SIDEWAYS?

5. TREND STRUCTURE
   - Higher High / Higher Low (uptrend)
   - Lower High / Lower Low (downtrend)
   - Range/Consolidation
   - Trend change if visible

6. SUPPORT LEVELS - Key support price levels

7. RESISTANCE LEVELS - Key resistance price levels

8. BREAKOUT/BREAKDOWN STATUS
   - Confirmed breakout/breakdown
   - Possible breakout/breakdown
   - None visible

9. LIQUIDITY PATTERNS
   - Liquidity sweeps
   - Rejections
   - Stop hunts

10. MOMENTUM INDICATORS
    - What does momentum show?
    - Is momentum confirming price action?

11. CALL/PUT BIAS
    - Should I buy CALLS or PUTS?
    - Explain reasoning

12. SCALPING SETUP
    - Is this a good scalp setup?
    - Why or why not?

13. ENTRY POINT
    - Only give exact entry if derivable from chart
    - Otherwise write "N/A"

14. STOP LOSS
    - Only if reasonably derivable
    - Where should stop loss be placed?

15. TARGET 1 (Profit Target)
    - First profit taking level

16. TARGET 2 (Profit Target)
    - Second profit taking level

17. RISK/REWARD RATIO
    - Calculate based on entry and targets

18. NEXT LIKELY MOVE
    - What's the next likely price action?
    - Give probability-based reasoning, NOT certainty

19. CONFIDENCE LEVEL
    - LOW / MEDIUM / HIGH

20. FINAL ACTION
    Choose EXACTLY ONE:
    ✅ BUY CALL
    ✅ BUY PUT
    ❌ WAIT (if setup unclear)

CRITICAL REMINDERS:
- Never hallucinate exact price levels
- If anything is unreadable, use "N/A"
- If setup is unclear, action must be WAIT
- This is technical analysis only - not financial advice"""

    # --------------------------------------------------------
    # OPENAI API PAYLOAD
    # --------------------------------------------------------

    payload = {
        "model": model,
        "max_tokens": 2000,
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
        raise RuntimeError(
            "⏱️ OpenAI request timed out. Please try again."
        )

    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            "🌐 Could not connect to OpenAI API. Check your internet connection."
        )

    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"🔗 OpenAI connection error: {str(e)[:200]}"
        )

    # --------------------------------------------------------
    # API ERROR HANDLING
    # --------------------------------------------------------

    if response.status_code >= 400:
        try:
            error_data = response.json()
            error_message = error_data.get("error", {}).get("message", "")
            
            if error_message:
                raise RuntimeError(
                    f"OpenAI API Error {response.status_code}: {error_message}"
                )
        except ValueError:
            pass

        raise RuntimeError(
            f"OpenAI API Error {response.status_code}: {response.text[:500]}"
        )

    # --------------------------------------------------------
    # PARSE JSON RESPONSE
    # --------------------------------------------------------

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            "OpenAI returned an invalid JSON response."
        )

    # --------------------------------------------------------
    # EXTRACT ANALYSIS REPORT
    # --------------------------------------------------------

    # Try new format first
    if data.get("content"):
        report_parts = []
        for content_block in data.get("content", []):
            if content_block.get("type") == "text":
                text = content_block.get("text", "")
                if text:
                    report_parts.append(text)
        report = "\n".join(report_parts).strip()
    else:
        report = ""

    if not report:
        raise RuntimeError(
            "AI returned an empty analysis report. Please try again."
        )

    return report


# ============================================================
# IMAGE UPLOAD HANDLER
# ============================================================

def _get_uploaded_image():
    """Handle chart image upload from file picker."""
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
            st.error(f"Error reading uploaded image: {e}")

    return None, None


# ============================================================
# CLIPBOARD PASTE HANDLER
# ============================================================

def _get_pasted_image():
    """Handle chart image paste from clipboard (optional)."""
    try:
        from streamlit_pastebutton import paste_image_button

        pasted = paste_image_button(
            label="📋 Paste chart from clipboard",
            key="ai_chart_paste_main"
        )

        if pasted:
            if isinstance(pasted, dict):
                image_bytes = pasted.get("bytes") or pasted.get("image")
                mime = pasted.get("mime_type", None)

                if image_bytes:
                    detected = _chart_image_mime(image_bytes)
                    return image_bytes, detected

    except ImportError:
        st.caption(
            "💡 Clipboard paste plugin not installed. Use Upload Chart instead."
        )
    except Exception as e:
        st.warning(f"Clipboard paste error: {e}")

    return None, None


# ============================================================
# MAIN APPLICATION
# ============================================================

def show_ai_chart_analysis():
    """Main chart analysis interface."""

    # ========================================================
    # HEADER
    # ========================================================

    st.markdown("# 🤖 AI CHART ANALYSIS")
    st.markdown("---")

    st.info(
        "📊 **Upload your trading chart** (TradingView, MT5, etc.) for AI-powered technical analysis. "
        "The analysis uses ONLY the supplied chart image."
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

    # ========================================================
    # IMAGE INPUT SECTION
    # ========================================================

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### Upload Image")
        uploaded_bytes, uploaded_mime = _get_uploaded_image()

        if uploaded_bytes:
            st.session_state["ai_chart_image"] = uploaded_bytes
            st.session_state["ai_chart_mime"] = uploaded_mime

    with col2:
        st.markdown("### Paste Image")
        pasted_bytes, pasted_mime = _get_pasted_image()

        if pasted_bytes:
            st.session_state["ai_chart_image"] = pasted_bytes
            st.session_state["ai_chart_mime"] = pasted_mime
            st.success("✅ Chart pasted successfully!")

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
        st.image(
            image_bytes,
            caption="This exact image will be submitted for analysis",
            use_container_width=True
        )

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
        with st.spinner("🧠 AI analyzing chart... This may take 30-60 seconds"):
            try:
                report = _analyze(image_bytes, mime)
                st.session_state["ai_chart_report"] = report
                st.success("✅ Chart analysis completed!")

            except Exception as e:
                st.session_state["ai_chart_report"] = ""
                st.error(f"❌ Analysis failed: {str(e)}")

    # ========================================================
    # HANDLE CLEAR ACTION
    # ========================================================

    if clear:
        st.session_state["ai_chart_image"] = None
        st.session_state["ai_chart_mime"] = None
        st.session_state["ai_chart_report"] = ""
        st.rerun()

    # ========================================================
    # DISPLAY REPORT
    # ========================================================

    report = st.session_state.get("ai_chart_report", "")

    if report:
        st.markdown("---")
        st.markdown("## 📋 Analysis Report")
        st.markdown(report)
        st.markdown("---")

        st.warning(
            "⚠️ **DISCLAIMER**: This is technical analysis only. "
            "Always verify price, entry, stop-loss, and risk management "
            "before taking ANY trade. Not financial advice."
        )

        # Download report
        st.download_button(
            label="📥 Download Report as Text",
            data=report,
            file_name="chart_analysis.txt",
            mime="text/plain"
        )


# ============================================================
# APP ENTRY POINT
# ============================================================

if __name__ == "__main__":
    show_ai_chart_analysis()
