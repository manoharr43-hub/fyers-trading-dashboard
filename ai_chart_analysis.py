import base64
import requests
import streamlit as st


# ============================================================
# OPENAI
# ============================================================

OPENAI_API_URL = "https://api.openai.com/v1/responses"


# ============================================================
# SECRET HELPER
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
# IMAGE MIME DETECTOR
# ============================================================

def _chart_image_mime(image_bytes):

    if not image_bytes:
        return "image/png"

    # PNG
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # JPG / JPEG
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # WEBP
    if (
        image_bytes.startswith(b"RIFF")
        and len(image_bytes) >= 12
        and image_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"

    # Default
    return "image/png"


# ============================================================
# OUTPUT TEXT
# ============================================================

def _output_text(data):

    if not isinstance(data, dict):
        return ""

    # Responses API output_text
    output_text = data.get("output_text")

    if output_text:
        return str(output_text).strip()

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

    # --------------------------------------------------------
    # CHECK IMAGE
    # --------------------------------------------------------

    if not image_bytes:
        raise RuntimeError(
            "No chart image was supplied."
        )

    # --------------------------------------------------------
    # API KEY
    # --------------------------------------------------------

    key = _secret("OPENAI_API_KEY")

    if not key:

        raise RuntimeError(
            "OPENAI_API_KEY missing in Streamlit Secrets."
        )

    # --------------------------------------------------------
    # MODEL
    # --------------------------------------------------------

    model = _secret(
        "OPENAI_MODEL",
        "gpt-5.6-luna"
    )

    # --------------------------------------------------------
    # MIME
    # --------------------------------------------------------

    mime = _chart_image_mime(image_bytes)

    # --------------------------------------------------------
    # BASE64
    # --------------------------------------------------------

    image64 = base64.b64encode(
        image_bytes
    ).decode("utf-8")

    # --------------------------------------------------------
    # PROMPT
    # --------------------------------------------------------

    prompt = """
You are an expert technical chart analyst.

IMPORTANT:

Analyze ONLY the supplied chart screenshot.

Do NOT use:
- NSE/F&O dropdown selection
- assumed stock
- external market data
- guessed prices
- guessed levels

If something is unreadable, write N/A.

Never invent exact price levels.

Provide the analysis in this exact structure:

1. INSTRUMENT
2. TIMEFRAME
3. CURRENT VISIBLE PRICE
4. DIRECTION
5. TREND STRUCTURE
6. SUPPORT
7. RESISTANCE
8. BREAKOUT / BREAKDOWN
9. LIQUIDITY SWEEP / REJECTION
10. MOMENTUM
11. CALL / PUT BIAS
12. SCALPING SETUP
13. ENTRY
14. STOP LOSS
15. TARGET 1
16. TARGET 2
17. RISK / REWARD
18. NEXT LIKELY MOVE
19. CONFIDENCE
20. FINAL ACTION

FINAL ACTION must be exactly one of:

BUY CALL
BUY PUT
WAIT

Rules:

- Do not hallucinate.
- Do not create unreadable prices.
- If the setup is unclear, use WAIT.
- If support/resistance cannot be read, use N/A.
- Entry, SL and targets should only be given when reasonably derivable from the visible chart.
- Clearly distinguish visible facts from analysis.
- Be concise and practical.
"""

    # --------------------------------------------------------
    # PAYLOAD
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
                        "image_url":
                            f"data:{mime};base64,{image64}"
                    }

                ]
            }

        ]
    }

    # --------------------------------------------------------
    # REQUEST
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
                    f"OpenAI API Error "
                    f"{response.status_code}: "
                    f"{error_message}"
                )

        except ValueError:

            pass

        raise RuntimeError(
            f"OpenAI API Error "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    try:

        data = response.json()

    except ValueError:

        raise RuntimeError(
            "OpenAI returned invalid JSON response."
        )

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    report = _output_text(data)

    if not report:

        raise RuntimeError(
            "AI returned an empty chart analysis."
        )

    return report


# ============================================================
# MAIN PAGE
# ============================================================

def show_ai_chart_analysis(fyers=None):

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    st.markdown(
        "## 🤖 AI CHART ANALYSIS"
    )

    st.info(
        "📷 Upload or paste your chart. "
        "NSE/F&O stock selection is NOT used."
    )

    # --------------------------------------------------------
    # SESSION STATE
    # --------------------------------------------------------

    if "ai_chart_image" not in st.session_state:

        st.session_state[
            "ai_chart_image"
        ] = None

    if "ai_chart_mime" not in st.session_state:

        st.session_state[
            "ai_chart_mime"
        ] = None

    if "ai_chart_report" not in st.session_state:

        st.session_state[
            "ai_chart_report"
        ] = ""

    # --------------------------------------------------------
    # UPLOAD
    # --------------------------------------------------------

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

    if uploaded:

        try:

            image_bytes = uploaded.getvalue()

            if image_bytes:

                mime = _chart_image_mime(
                    image_bytes
                )

                st.session_state[
                    "ai_chart_image"
                ] = image_bytes

                st.session_state[
                    "ai_chart_mime"
                ] = mime

        except Exception as e:

            st.error(
                f"Image loading failed: {e}"
            )

    # --------------------------------------------------------
    # PASTE BUTTON
    # --------------------------------------------------------

    try:

        from streamlit_pastebutton import (
            paste_image_button
        )

        pasted = paste_image_button(

            label="📋 Paste chart from clipboard",

            key="ai_chart_paste_main"
        )

        if pasted and isinstance(pasted, dict):

            pasted_bytes = (
                pasted.get("bytes")
                or pasted.get("image")
            )

            if pasted_bytes:

                mime = _chart_image_mime(
                    pasted_bytes
                )

                st.session_state[
                    "ai_chart_image"
                ] = pasted_bytes

                st.session_state[
                    "ai_chart_mime"
                ] = mime

                st.success(
                    "✅ Chart pasted successfully."
                )

    except ImportError:

        st.caption(
            "Clipboard paste is unavailable. "
            "Please use Upload Chart."
        )

    except Exception:

        pass

    # --------------------------------------------------------
    # GET CURRENT IMAGE
    # --------------------------------------------------------

    image_bytes = st.session_state.get(
        "ai_chart_image"
    )

    mime = st.session_state.get(
        "ai_chart_mime"
    )

    # --------------------------------------------------------
    # PREVIEW
    # --------------------------------------------------------

    if image_bytes:

        st.markdown(
            "### 👁️ CHART PREVIEW"
        )

        st.image(

            image_bytes,

            caption=(
                "Preview — this exact image "
                "will be submitted"
            ),

            use_container_width=True
        )

    # --------------------------------------------------------
    # SUBMIT
    # --------------------------------------------------------

    submit = st.button(

        "🧠 SUBMIT CHART → ANALYZE",

        type="primary",

        use_container_width=True,

        disabled=not bool(image_bytes),

        key="ai_chart_submit_main"
    )

    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

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

        st.caption(
            "⚠️ Informational only. "
            "Verify levels and risk before trading."
        )

    # --------------------------------------------------------
    # RE-ANALYZE
    # --------------------------------------------------------

    if image_bytes:

        st.markdown("---")

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
                        "✅ Re-analysis completed."
                    )

                except Exception as e:

                    st.error(
                        f"❌ Chart analysis failed: {e}"
                    )

    # --------------------------------------------------------
    # CLEAR
    # --------------------------------------------------------

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
