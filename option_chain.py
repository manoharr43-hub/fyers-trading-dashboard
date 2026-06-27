import streamlit as st
import pandas as pd


def show_option_chain(fyers):

    st.title("⚙️ Option Chain")

    index = st.selectbox(
        "Select Index",
        [
            "NSE:NIFTY50-INDEX",
            "NSE:NIFTYBANK-INDEX",
            "NSE:FINNIFTY-INDEX"
        ]
    )

    expiry = st.text_input(
        "Expiry (YYYY-MM-DD)",
        "2026-07-30"
    )

    if st.button("Load Option Chain"):

        try:

            data = {
                "symbol": index,
                "expiry": expiry
            }

            # FYERS Option Chain API
            response = fyers.optionchain(data)

            if response.get("s") == "ok":

                options = response.get("data", [])

                if len(options):

                    df = pd.DataFrame(options)

                    st.success("Option Chain Loaded")

                    st.dataframe(
                        df,
                        use_container_width=True,
                        hide_index=True
                    )

                    csv = df.to_csv(index=False)

                    st.download_button(
                        "⬇ Download CSV",
                        csv,
                        "option_chain.csv",
                        "text/csv"
                    )

                else:

                    st.warning("No Data Found")

            else:

                st.error(response)

        except Exception as e:

            st.error(
                "Option Chain API is not enabled or not available."
            )

            st.exception(e)
