import base64
import requests
import streamlit as st


OPENAI_API_URL = "https://api.openai.com/v1/responses"


# =========================================================
# STREAMLIT SECRETS
# =========================================================
def _secret(name, default=""):
    try:
        value = st.secrets.get(name, default)
        if value is None:
            return default
        return str(value).strip()
    except Exception:
        return default


# =========================================================
# IMAGE MIME DETECTION
# =========================================================
def _chart_image_mime(image_bytes: bytes) -> str:
    """
    Detect chart screenshot MIME type from image bytes.
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
        len(image_bytes) >= 12
        and image_bytes[:4] == b"RIFF"
        and image_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"

    # Fallback
    return "image/png"


# =========================================================
# OPENAI RESPONSE TEXT EXTRACTION
# =========================================================
def _output_text(data):
    """
    Extract text from OpenAI Responses API response.
    """

    if not isinstance(data, dict):
        return ""

    # Direct output_text
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


# =========================================================
# OPENAI CHART ANALYSIS
# =========================================================
def _analyze(image_bytes, mime=None):

    if not image_bytes:
        raise RuntimeError("No chart image received.")

    # Get API key
    key = _secret("OPENAI_API_KEY")

    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY missing in Streamlit Secrets."
        )

    # Model
    model = _secret(
        "OPENAI_MODEL",
        "gpt-5.6-luna"
    )

    # Detect MIME safely
    detected_mime = _chart_image_mime(image_bytes)

    if mime:
        mime = str(mime).lower().strip()

        allowed = {
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/webp",
        }

        if mime in allowed:

            if mime == "image/jpg":
                mime = "image/jpeg"

        else:
            mime = detected_mime

    else:
        mime = detected_mime

    # Base64
    image64 = base64.b64encode(image_bytes).decode("utf-8")

    # =====================================================
    # CHART ANALYSIS PROMPT
    # =====================================================
    prompt = """
Analyze ONLY the supplied trading chart screenshot.

IMPORTANT RULES:

1. Do NOT use any NSE/F&O dropdown selection.
2. Do NOT assume any stock or index name.
3. Use ONLY information visible in the supplied chart.
4. If a value is unreadable, write N/A.
5. Never invent exact prices or levels.
6. Do not pretend to know hidden indicators.
7. Keep the answer practical and concise.

Give the following:

1. Instrument / symbol if visible
2. Timeframe if visible
3. Current visible price
4. Direction: BULLISH / BEARISH / SIDEWAYS
5. Trend structure
6. Higher High / Higher Low or Lower High / Lower Low if visible
7. Support
8. Resistance
9. Breakout / Breakdown status
10. Liquidity sweep / rejection if visible
11. CALL / PUT / WAIT bias
12. Reason for the CALL / PUT bias
13. Scalping setup
14. Entry only when safely readable or derivable
15. Stop Loss only when safely readable or derivable
16. Target 1 only when safely readable or derivable
17. Target 2 only when safely readable or derivable
18. Risk / Reward
19. Next likely move
20. Confidence percentage
21. FINAL ACTION: BUY CALL / BUY PUT / WAIT

Do not hallucinate exact levels.
If chart evidence is insufficient, choose WAIT.
"""

    # =====================================================
    # OPENAI RESPONSES API PAYLOAD
    # =====================================================
    payload = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    },
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:{mime};base64,{image64}"
                        ),
                    },
                ],
            }
        ],
    }

    # =====================================================
    # API REQUEST
    # =====================================================
    try:

        response = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )

    except requests.exceptions.Timeout:
        raise RuntimeError(
            "OpenAI API request timed out. Please try again."
        )

    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"Network error while contacting OpenAI: {e}"
        )

    # =====================================================
    # API ERROR
    # =====================================================
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
            f"{response.text}"
        )

    # =====================================================
    # PARSE RESPONSE
    # =====================================================
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            "OpenAI returned an invalid JSON response."
        )

    text = _output_text(data)

    if not text:
        raise RuntimeError(
            "AI returned an empty chart analysis report."
        )

    return text


# =========================================================
# STREAMLIT UI
# =========================================================
def show_ai_chart_analysis(fyers=None):

    st.markdown("## 🤖 AI CHART ANALYSIS")

    st.info(
        "📷 Upload or paste your chart. "
        "NSE/F&O stock selection is NOT used for this analysis."
    )

    # =====================================================
    # FILE UPLOAD
    # =====================================================
    uploaded = st.file_uploader(
        "📷 Upload chart screenshot",
        type=[
            "png",
            "jpg",
            "jpeg",
            "webp",
        ],
        key="ai_chart_upload_main",
    )

    image_bytes = None
    mime = None

    if uploaded:

        try:
            image_bytes = uploaded.getvalue()
            mime = uploaded.type

        except Exception:
            image_bytes = None
            mime = None

    # =====================================================
    # PASTE IMAGE
    # =====================================================
    try:

        from streamlit_pastebutton import paste_image_button

        pasted = paste_image_button(
            label="📋 Paste chart from clipboard",
            key="ai_chart_paste_main",
        )

        if pasted:

            if isinstance(pasted, dict):

                pasted_bytes = (
                    pasted.get("bytes")
                    or pasted.get("image")
                )

                if pasted_bytes:
                    image_bytes = pasted_bytes

                pasted_mime = pasted.get("mime_type")

                if pasted_mime:
                    mime = pasted_mime

            elif isinstance(pasted, bytes):

                image_bytes = pasted
                mime = _chart_image_mime(pasted)

    except Exception:
        pass

    # =====================================================
    # AUTO DETECT MIME
    # =====================================================
    if image_bytes:

        detected_mime = _chart_image_mime(image_bytes)

        if not mime:
            mime = detected_mime

        if mime == "image/jpg":
            mime = "image/jpeg"

        if mime not in {
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            mime = detected_mime

    # =====================================================
    # PREVIEW
    # =====================================================
    if image_bytes:

        st.image(
            image_bytes,
            caption="Chart to analyze",
            use_container_width=True,
        )

    # =====================================================
    # SUBMIT BUTTON
    # =====================================================
    submit = st.button(
        "🧠 SMART SUBMIT → ANALYZE THIS CHART",
        type="primary",
        use_container_width=True,
        disabled=not bool(image_bytes),
        key="ai_chart_submit_main",
    )

    # =====================================================
    # ANALYZE
    # =====================================================
    if submit and image_bytes:

        with st.spinner(
            "🧠 AI analyzing chart..."
        ):

            try:

                report = _analyze(
                    image_bytes,
                    mime,
                )

                st.session_state[
                    "ai_chart_report"
                ] = report

                st.session_state[
                    "ai_chart_error"
                ] = ""

            except Exception as e:

                st.session_state[
                    "ai_chart_error"
                ] = str(e)

                st.error(
                    f"Chart analysis failed: {e}"
                )

    # =====================================================
    # ERROR DISPLAY
    # =====================================================
    saved_error = st.session_state.get(
        "ai_chart_error",
        "",
    )

    if saved_error:

        st.error(
            f"Chart analysis failed: {saved_error}"
        )

    # =====================================================
    # REPORT
    # =====================================================
    report = st.session_state.get(
        "ai_chart_report"
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

        # =================================================
        # RE-ANALYZE BUTTON
        # =================================================
        if st.button(
            "🔄 RE-ANALYZE CHART",
            use_container_width=True,
            key="ai_chart_reanalyze_main",
        ):

            if image_bytes:

                with st.spinner(
                    "🧠 Re-analyzing chart..."
                ):

                    try:

                        new_report = _analyze(
                            image_bytes,
                            mime,
                        )

                        st.session_state[
                            "ai_chart_report"
                        ] = new_report

                        st.rerun()

                    except Exception as e:

                        st.error(
                            f"Re-analysis failed: {e}"
                        )
