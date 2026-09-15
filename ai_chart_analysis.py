import base64
import requests
import streamlit as st


# ============================================================
# OPENAI CONFIG
# ============================================================

OPENAI_API_URL = "https://api.openai.com/v1/responses"


# ============================================================
# STREAMLIT SECRETS HELPER
# ============================================================

def _secret(name, default=""):
    try:
        value = st.secrets.get(name, default)
        if value is None:
            return default
        return str(value).strip()
    except Exception:
        return default


# ============================================================
# IMAGE MIME DETECTION
# FIXES:
# NameError: name '_chart_image_mime' is not defined
# ============================================================

def _chart_image_mime(image_bytes):
    """
    Detect image MIME type from actual file bytes.
    """

    if not image_bytes:
        return "image/png"

    # PNG
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # JPEG
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # WEBP
    if (
        image_bytes.startswith(b"RIFF")
        and len(image_bytes) >= 12
        and image_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"

    # Fallback
    return "image/png"


# ============================================================
# OUTPUT TEXT EXTRACTOR
# ============================================================

def _output_text(data):
    """
    Extract text from OpenAI Responses API response.
    """

    # New Responses API convenience field
    if data.get("output_text"):
        return str(data["output_text"]).strip()

    parts = []

    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue

        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue

            if content.get("type") == "output_text":
                text = content.get("text")

                if text:
                    parts.append(str(text))

    return "\n".join(parts).strip()


# ============================================================
# OPENAI CHART ANALYSIS
# ============================================================

def _analyze(image_bytes, mime=None):

    if not image_bytes:
        raise RuntimeError("No chart image was supplied.")

    # --------------------------------------------------------
    # API KEY
    # --------------------------------------------------------

    key = _secret("OPENAI_API_KEY")

    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. "
            "Add OPENAI_API_KEY to Streamlit Secrets."
        )

    # --------------------------------------------------------
    # MODEL
    # --------------------------------------------------------

    model = _secret(
        "OPENAI_MODEL",
        "gpt-5.6-luna"
    )

    # --------------------------------------------------------
    # MIME TYPE
    # --------------------------------------------------------

    detected_mime = _chart_image_mime(image_bytes)

    if not mime or not str(mime).startswith("image/"):
        mime = detected_mime

    # Always trust actual file bytes when possible
    if detected_mime != "image/png" or mime == "image/png":
        mime = detected_mime

    # --------------------------------------------------------
    # BASE64
    # --------------------------------------------------------

    image64 = base64.b64encode(image_bytes).decode("utf-8")

    # --------------------------------------------------------
    # CHART ANALYSIS PROMPT
    # --------------------------------------------------------

    prompt = """
You are an expert technical chart analyst.

IMPORTANT RULES:

1. Analyze ONLY the supplied chart image.
2. Do NOT use NSE/F&O dropdown selections.
3. Do NOT assume the instrument if it is not visible.
4. Do NOT invent prices or levels.
5. If something cannot be read clearly, write N/A.
6. Do not fabricate candle values.
7. Do not claim certainty about future price movement.
8. Give practical technical analysis based only on visible information.
9. Clearly separate visible facts from inference.
10. If the chart is unclear, say that the chart is unclear.

Analyze the visible chart and provide:

1. INSTRUMENT
2. TIMEFRAME
3. CURRENT VISIBLE PRICE
4. MARKET DIRECTION
   - BULLISH
   - BEARISH
   - SIDEWAYS

5. TREND STRUCTURE
   - Higher High / Higher Low
   - Lower High / Lower Low
   - Range
   - Trend change if visible

6. SUPPORT LEVELS

7. RESISTANCE LEVELS

8. BREAKOUT / BREAKDOWN
   - Confirmed
   - Possible
   - None visible

9. LIQUIDITY
   - Liquidity sweep if visible
   - Rejection if visible
   - Stop hunt if reasonably visible

10. MOMENTUM

11. CALL / PUT BIAS
    Explain why.

12. SCALPING SETUP

13. ENTRY
    Only give an exact entry if it can be safely derived from the visible chart.

14. STOP LOSS
    Only if reasonably derivable.

15. TARGET 1

16. TARGET 2

17. RISK / REWARD

18. NEXT LIKELY MOVE
    Give probability-based reasoning, not certainty.

19. CONFIDENCE
    LOW / MEDIUM / HIGH

20. FINAL ACTION
    Choose exactly one:

    BUY CALL
    BUY PUT
    WAIT

IMPORTANT:
Never hallucinate exact levels.
If price or levels are unreadable, use N/A.
If the setup is not clear, FINAL ACTION must be WAIT.
"""


    # --------------------------------------------------------
    # OPENAI RESPONSES PAYLOAD
    # --------------------------------------------------------

    payload = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt
                    },
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:{mime};base64,{image64}"
                        )
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
            "OpenAI request timed out. Please try again."
        )

    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            "Could not connect to OpenAI API."
        )

    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"OpenAI connection error: {e}"
        )

    # --------------------------------------------------------
    # API ERROR
    # --------------------------------------------------------

    if response.status_code >= 400:

        try:
            error_data = response.json()

            error_message = (
                error_data
                .get("error", {})
                .get("message")
            )

            if error_message:
                raise RuntimeError(
                    f"OpenAI API Error {response.status_code}: "
                    f"{error_message}"
                )

        except ValueError:
            pass

        raise RuntimeError(
            f"OpenAI API Error {response.status_code}: "
            f"{response.text[:1000]}"
        )

    # --------------------------------------------------------
    # JSON RESPONSE
    # --------------------------------------------------------

    try:
        data = response.json()

    except ValueError:
        raise RuntimeError(
            "OpenAI returned an invalid response."
        )

    # --------------------------------------------------------
    # EXTRACT REPORT
    # --------------------------------------------------------

    report = _output_text(data)

    if not report:
        raise RuntimeError(
            "AI returned an empty chart analysis report."
        )

    return report


# ============================================================
# IMAGE INPUT
# ============================================================

def _get_uploaded_image():

    uploaded = st.file_uploader(
        "📷 Upload chart screenshot",
        type=[
            "png",
            "jpg",
            "jpeg",
            "webp"
        ],
        key="ai_chart_upload_main"
    )

    if uploaded is not None:

        try:
            image_bytes = uploaded.getvalue()

            if image_bytes:
                mime = _chart_image_mime(image_bytes)

                return image_bytes, mime

        except Exception:
            pass

    return None, None


# ============================================================
# CLIPBOARD PASTE
# ============================================================

def _get_pasted_image():

    try:

        from streamlit_pastebutton import paste_image_button

        pasted = paste_image_button(
            label="📋 Paste chart from clipboard",
            key="ai_chart_paste_main"
        )

        if pasted:

            if isinstance(pasted, dict):

                image_bytes = (
                    pasted.get("bytes")
                    or pasted.get("image")
                )

                mime = pasted.get(
                    "mime_type",
                    None
                )

                if image_bytes:

                    detected = _chart_image_mime(
                        image_bytes
                    )

                    return (
                        image_bytes,
                        detected
                    )

    except ImportError:

        st.caption(
            "📋 Clipboard paste plugin is not installed. "
            "Use Upload Chart instead."
        )

    except Exception:
        pass

    return None, None


# ============================================================
# MAIN AI CHART ANALYSIS PAGE
# ============================================================

def show_ai_chart_analysis(fyers=None):

    # ========================================================
    # HEADER
    # ========================================================

    st.markdown(
        "## 🤖 AI CHART ANALYSIS"
    )

    st.info(
        "📷 Upload or paste your TradingView chart. "
        "This analysis uses the supplied chart image only."
    )

    # ========================================================
    # SESSION STATE
    # ========================================================

    if "ai_chart_report" not in st.session_state:
        st.session_state["ai_chart_report"] = ""

    if "ai_chart_image" not in st.session_state:
        st.session_state["ai_chart_image"] = None

    if "ai_chart_mime" not in st.session_state:
        st.session_state["ai_chart_mime"] = None

    # ========================================================
    # UPLOAD
    # ========================================================

    uploaded_bytes, uploaded_mime = _get_uploaded_image()

    if uploaded_bytes:

        st.session_state["ai_chart_image"] = uploaded_bytes
        st.session_state["ai_chart_mime"] = uploaded_mime

    # ========================================================
    # PASTE
    # ========================================================

    pasted_bytes, pasted_mime = _get_pasted_image()

    if pasted_bytes:

        st.session_state["ai_chart_image"] = pasted_bytes
        st.session_state["ai_chart_mime"] = pasted_mime

        st.success(
            "✅ Chart pasted successfully."
        )

    # ========================================================
    # CURRENT IMAGE
    # ========================================================

    image_bytes = st.session_state.get(
        "ai_chart_image"
    )

    mime = st.session_state.get(
        "ai_chart_mime"
    )

    # ========================================================
    # IMAGE PREVIEW
    # ========================================================

    if image_bytes:

        st.markdown(
            "### 👁️ CHART PREVIEW"
        )

        st.image(
            image_bytes,
            caption="Chart to analyze",
            use_container_width=True
        )

        st.caption(
            "Preview — this exact image will be submitted."
        )

    # ========================================================
    # SUBMIT BUTTON
    # ========================================================

    submit = st.button(
        "🧠 SUBMIT CHART → ANALYZE",
        type="primary",
        use_container_width=True,
        disabled=not bool(image_bytes),
        key="ai_chart_submit_main"
    )

    # ========================================================
    # ANALYZE
    # ========================================================

    if submit and image_bytes:

        with st.spinner(
            "🧠 AI analyzing chart..."
        ):

            try:

                report = _analyze(
                    image_bytes,
                    mime
                )

                st.session_state[
                    "ai_chart_report"
                ] = report

                st.success(
                    "✅ Chart analysis completed."
                )

            except Exception as e:

                st.session_state[
                    "ai_chart_report"
                ] = ""

                st.error(
                    f"❌ Chart analysis failed: {e}"
                )

    # ========================================================
    # RE-ANALYZE BUTTON
    # ========================================================

    if image_bytes:

        st.markdown("")

        reanalyze = st.button(
            "🔄 RE-ANALYZE CHART",
            use_container_width=True,
            key="ai_chart_reanalyze"
        )

        if reanalyze:

            with st.spinner(
                "🧠 Re-analyzing chart..."
            ):

                try:

                    report = _analyze(
                        image_bytes,
                        mime
                    )

                    st.session_state[
                        "ai_chart_report"
                    ] = report

                    st.success(
                        "✅ Chart re-analysis completed."
                    )

                except Exception as e:

                    st.error(
                        f"❌ Chart analysis failed: {e}"
                    )

    # ========================================================
    # REPORT
    # ========================================================

    report = st.session_state.get(
        "ai_chart_report",
        ""
    )

    if report:

        st.markdown("---")

        st.markdown(
            "## 📋 CHART ANALYSIS REPORT"
        )

        st.markdown(report)

        st.markdown("---")

        st.warning(
            "⚠️ Informational analysis only. "
            "Verify price, entry, stop-loss and risk "
            "before taking any trade."
        )

    # ========================================================
    # CLEAR BUTTON
    # ========================================================

    if image_bytes or report:

        st.markdown("")

        clear = st.button(
            "🗑️ CLEAR CHART & REPORT",
            use_container_width=True,
            key="ai_chart_clear"
        )

        if clear:

            st.session_state[
                "ai_chart_image"
            ] = None

            st.session_state[
                "ai_chart_mime"
            ] = None

            st.session_state[
                "ai_chart_report"
            ] = ""

            st.rerun()
