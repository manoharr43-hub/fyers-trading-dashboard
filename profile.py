import streamlit as st
import pandas as pd
import json


# =====================================
# PROFILE
# =====================================
def show_profile(fyers):

    st.title("👤 Profile & Funds")

    tab1, tab2 = st.tabs(
        [
            "👤 Profile",
            "💰 Funds"
        ]
    )

    # =====================================
    # PROFILE
    # =====================================
    with tab1:

        try:

            profile = fyers.get_profile()

            if profile.get("s") == "ok":

                data = profile.get("data", {})

                col1, col2 = st.columns(2)

                with col1:

                    st.metric(
                        "Client ID",
                        data.get("fy_id", "-")
                    )

                    st.metric(
                        "Name",
                        data.get("display_name", "-")
                    )

                    st.metric(
                        "Email",
                        data.get("email_id", "-")
                    )

                with col2:

                    st.metric(
                        "Mobile",
                        data.get("mobile_number", "-")
                    )

                    st.metric(
                        "PAN",
                        data.get("pan", "-")
                    )

                    st.metric(
                        "Status",
                        data.get("status", "-")
                    )

                st.divider()

                st.subheader("Complete Profile")

                st.json(profile)

                st.download_button(
                    "⬇ Download Profile",
                    json.dumps(profile, indent=4),
                    "profile.json",
                    "application/json"
                )

            else:

                st.error(profile)

        except Exception as e:

            st.error(e)

    # =====================================
    # FUNDS
    # =====================================
    with tab2:

        try:

            funds = fyers.funds()

            if funds.get("s") == "ok":

                st.subheader("Available Funds")

                st.json(funds)

                fund_list = funds.get("fund_limit", [])

                if len(fund_list):

                    df = pd.DataFrame(fund_list)

                    st.dataframe(
                        df,
                        use_container_width=True,
                        hide_index=True
                    )

                    st.download_button(
                        "⬇ Download Funds",
                        df.to_csv(index=False),
                        "funds.csv",
                        "text/csv"
                    )

            else:

                st.error(funds)

        except Exception as e:

            st.error(e)

    st.divider()

    if st.button("🔄 Refresh"):

        st.rerun()
