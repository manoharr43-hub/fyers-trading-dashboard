import streamlit as st
import pandas as pd
from datetime import datetime


# ==========================================
# SUCCESS MESSAGE
# ==========================================
def success(message):
    st.success(message)


# ==========================================
# ERROR MESSAGE
# ==========================================
def error(message):
    st.error(message)


# ==========================================
# WARNING MESSAGE
# ==========================================
def warning(message):
    st.warning(message)


# ==========================================
# INFO MESSAGE
# ==========================================
def info(message):
    st.info(message)


# ==========================================
# DATAFRAME
# ==========================================
def show_dataframe(df):

    if isinstance(df, pd.DataFrame):
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True
        )
    else:
        st.warning("No Data Available")


# ==========================================
# CSV DOWNLOAD
# ==========================================
def download_csv(df, filename):

    if isinstance(df, pd.DataFrame):

        st.download_button(
            label="⬇ Download CSV",
            data=df.to_csv(index=False),
            file_name=filename,
            mime="text/csv"
        )


# ==========================================
# JSON VIEW
# ==========================================
def show_json(data):
    st.json(data)


# ==========================================
# PAGE TITLE
# ==========================================
def page_title(title, icon="📈"):
    st.title(f"{icon} {title}")


# ==========================================
# METRIC CARD
# ==========================================
def metric(title, value, delta=None):

    st.metric(
        label=title,
        value=value,
        delta=delta
    )


# ==========================================
# AUTO REFRESH INFO
# ==========================================
def refresh_info():

    now = datetime.now()

    st.caption(
        f"Last Updated : {now.strftime('%d-%m-%Y %H:%M:%S')}"
    )


# ==========================================
# SIDEBAR HEADER
# ==========================================
def sidebar_header():

    st.sidebar.image(
        "https://fyers.in/community/uploads/default/original/2X/5/5fd2c0d6d0c7d2.png",
        width=120
    )

    st.sidebar.title("FYERS Dashboard")


# ==========================================
# FOOTER
# ==========================================
def footer():

    st.markdown("---")

    st.caption(
        "Developed using FYERS API V3 + Streamlit"
    )


# ==========================================
# EMPTY DATA MESSAGE
# ==========================================
def no_data():
    st.info("No Data Found")


# ==========================================
# LOADING
# ==========================================
def loading(text="Loading..."):

    return st.spinner(text)
