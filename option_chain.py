import streamlit as st
import pandas as pd


def show_option_chain(fyers):

    st.title("📊 Option Chain")

    index = st.selectbox(
        "Select Index",
        [
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
            "SENSEX"
        ]
    )

    symbols = {
        "NIFTY": "NSE:NIFTY50-INDEX",
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX"
    }

    strike_count = st.slider(
        "Strike Count",
        5,
        25,
        10
    )

    if st.button("Load Option Chain", use_container_width=True):

        with st.spinner("Loading Option Chain..."):

            try:

                response = fyers.optionchain({

                    "symbol": symbols[index],

                    "strikecount": strike_count

                })

                if response.get("s") == "ok":

                    st.success("Option Chain Loaded")

                    # Raw Response
                    st.json(response)

                    # Option Data Table
                    if "data" in response:

                        try:

                            df = pd.DataFrame(response["data"])

                            st.dataframe(
                                df,
                                use_container_width=True
                            )

                            st.download_button(
                                "⬇ Download CSV",
                                df.to_csv(index=False),
                                "option_chain.csv",
                                "text/csv"
                            )

                        except:

                            pass

                else:

                    st.error(response)

            except Exception as e:

                st.error(e)

    st.divider()

    if st.button("🔄 Refresh"):

        st.rerun()
