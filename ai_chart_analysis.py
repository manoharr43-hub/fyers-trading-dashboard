import base64
import requests
import streamlit as st

OPENAI_API_URL = "https://api.openai.com/v1/responses"

def _secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

def _output_text(data):
    if data.get("output_text"):
        return data["output_text"]
    parts = []
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text" and c.get("text"):
                parts.append(c["text"])
    return "\n".join(parts).strip()

def _analyze(image_bytes, mime):
    key = _secret("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing in Streamlit secrets.")

    model = _secret("OPENAI_MODEL", "gpt-5.6-luna")
    image64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt = """Analyze ONLY the supplied chart screenshot.
Do not use any NSE/F&O dropdown or assumed stock.
If a value is unreadable, say N/A; never invent values.

Give:
1. Instrument/timeframe if visible
2. Current visible price
3. Direction: BULLISH/BEARISH/SIDEWAYS
4. Trend structure
5. Support
6. Resistance
7. Breakout/breakdown
8. Liquidity sweep/rejection if visible
9. CALL/PUT bias with reason
10. Scalping setup
11. Entry, SL, Target 1, Target 2 only when safely readable/derivable
12. Risk-reward
13. Next likely move
14. Confidence
15. FINAL ACTION: BUY CALL / BUY PUT / WAIT

Be concise and practical. Never hallucinate exact levels."""
    payload = {
        "model": model,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image",
                 "image_url": f"data:{mime};base64,{image64}"}
            ]
        }]
    }
    r = requests.post(
        OPENAI_API_URL,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI API Error {r.status_code}: {r.text}")
    text = _output_text(r.json())
    if not text:
        raise RuntimeError("AI returned an empty report.")
    return text

def show_ai_chart_analysis(fyers=None):
    st.markdown("## 🤖 AI CHART ANALYSIS")
    st.info("📷 Upload your chart. NSE/F&O stock selection is NOT used here.")

    uploaded = st.file_uploader(
        "📷 Upload chart screenshot",
        type=["png", "jpg", "jpeg", "webp"],
        key="ai_chart_upload_main",
    )

    image_bytes = uploaded.getvalue() if uploaded else None
    mime = (uploaded.type if uploaded else "image/png")

    try:
        from streamlit_pastebutton import paste_image_button
        pasted = paste_image_button(
            label="📋 Paste chart from clipboard",
            key="ai_chart_paste_main",
        )
        if pasted and isinstance(pasted, dict):
            image_bytes = pasted.get("bytes") or pasted.get("image")
            mime = pasted.get("mime_type", "image/png")
    except Exception:
        pass

    if image_bytes:
        st.image(image_bytes, caption="Chart to analyze", use_container_width=True)

    submit = st.button(
        "🧠 SMART SUBMIT → ANALYZE THIS CHART",
        type="primary",
        use_container_width=True,
        disabled=not bool(image_bytes),
        key="ai_chart_submit_main",
    )

    if submit and image_bytes:
        with st.spinner("🧠 AI analyzing chart..."):
            try:
                st.session_state["ai_chart_report"] = _analyze(image_bytes, mime)
            except Exception as e:
                st.error(f"Chart analysis failed: {e}")

    if st.session_state.get("ai_chart_report"):
        st.markdown("---")
        st.markdown("## 📋 CHART ANALYSIS REPORT")
        st.markdown(st.session_state["ai_chart_report"])
        st.caption("⚠️ Informational only. Verify levels and risk before trading.")
