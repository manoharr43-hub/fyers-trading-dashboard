import streamlit as st
import pandas as pd


def show_orders(fyers):

    st.title("📋 Orders & Trades")

    tab1, tab2, tab3 = st.tabs(
        ["📋 Order Book", "📑 Trade Book", "⚙️ Manage Order"]
    )

    # =====================================
    # ORDER BOOK
    # =====================================
    with tab1:

        try:

            response = fyers.orderbook()

            orders = response.get("orderBook", [])

            if orders:

                df = pd.DataFrame(orders)

                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True
                )

                st.download_button(
                    "⬇ Export Orders",
                    df.to_csv(index=False),
                    "orders.csv",
                    "text/csv"
                )

            else:
                st.info("No Orders Found")

        except Exception as e:
            st.error(e)

    # =====================================
    # TRADE BOOK
    # =====================================
    with tab2:

        try:

            response = fyers.tradebook()

            trades = response.get("tradeBook", [])

            if trades:

                df = pd.DataFrame(trades)

                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True
                )

                st.download_button(
                    "⬇ Export Trades",
                    df.to_csv(index=False),
                    "trades.csv",
                    "text/csv"
                )

            else:
                st.info("No Trades Found")

        except Exception as e:
            st.error(e)

    # =====================================
    # MANAGE ORDERS
    # =====================================
    with tab3:

        st.subheader("Cancel Order")

        order_id = st.text_input("Order ID")

        if st.button("❌ Cancel Order"):

            if order_id:

                try:

                    data = {
                        "id": order_id
                    }

                    response = fyers.cancel_order(data)

                    st.write(response)

                except Exception as e:
                    st.error(e)

        st.divider()

        st.subheader("Modify Order")

        modify_order_id = st.text_input("Modify Order ID")

        qty = st.number_input(
            "Quantity",
            min_value=1,
            value=1
        )

        price = st.number_input(
            "Price",
            min_value=0.0,
            value=0.0
        )

        if st.button("✏️ Modify Order"):

            try:

                data = {
                    "id": modify_order_id,
                    "qty": qty,
                    "limitPrice": price
                }

                response = fyers.modify_order(data)

                st.write(response)

            except Exception as e:
                st.error(e)
