import streamlit as st


# ==========================================
# BUY / SELL PANEL
# ==========================================
def show_trading(fyers):

    st.title("💹 Buy / Sell")

    col1, col2 = st.columns(2)

    with col1:

        symbol = st.text_input(
            "Symbol",
            "NSE:RELIANCE-EQ"
        )

        qty = st.number_input(
            "Quantity",
            min_value=1,
            value=1
        )

        order_type = st.selectbox(
            "Order Type",
            [
                "MARKET",
                "LIMIT"
            ]
        )

        side = st.selectbox(
            "Action",
            [
                "BUY",
                "SELL"
            ]
        )

        product = st.selectbox(
            "Product",
            [
                "CNC",
                "INTRADAY"
            ]
        )

        limit_price = 0.0

        if order_type == "LIMIT":

            limit_price = st.number_input(
                "Limit Price",
                min_value=0.0,
                value=0.0
            )

        stop_price = st.number_input(
            "Stop Price",
            min_value=0.0,
            value=0.0
        )

    with col2:

        st.info("Verify order details before placing the order.")

        if st.button("🚀 Place Order", use_container_width=True):

            try:

                data = {

                    "symbol": symbol,

                    "qty": int(qty),

                    # BUY = 1
                    # SELL = -1
                    "side": 1 if side == "BUY" else -1,

                    # MARKET = 2
                    # LIMIT = 1
                    "type": 2 if order_type == "MARKET" else 1,

                    "productType":
                        "INTRADAY"
                        if product == "INTRADAY"
                        else "CNC",

                    "limitPrice":
                        float(limit_price),

                    "stopPrice":
                        float(stop_price),

                    "validity":
                        "DAY",

                    "disclosedQty": 0,

                    "offlineOrder": False,

                    "takeProfit": 0,

                    "stopLoss": 0

                }

                response = fyers.place_order(data)

                st.success("Order Submitted")

                st.write(response)

            except Exception as e:

                st.error(e)

    st.divider()

    st.subheader("Order Parameters")

    st.code(
        """
BUY  = side : 1
SELL = side : -1

LIMIT = type : 1
MARKET = type : 2

Product:
CNC
INTRADAY
"""
    )
