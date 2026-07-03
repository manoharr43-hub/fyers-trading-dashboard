tab_scanner, tab_intraday, tab_swing, tab_fo, \
    tab_intraday_cisd, tab_fo_cisd, tab_golden_death, tab_premarket, tab_fo_15m_cisd, \
    tab_order_block = st.tabs(
    ["📊 Full Scanner", "⚡ Intraday Scanner", "📈 Swing Trade Scanner", "🏛️ F&O Stocks Scanner",
     "🕐 Intraday CISD Signals", "🎯 F&O CISD Scanner", "✝️ Swing Trading (Golden/Death Cross)",
     "🌅 Pre-Market Scanner", "🎯 NSE F&O 15-Min CISD Scanner", "📦 Order Block Scanner"]
)


# ═══════════════════════════════════════════════════════════════════════
# EDIT B — add this whole block as a NEW tab, anywhere among the other
# "with tab_xxx:" blocks inside show_scanner() (e.g. right after the
# "with tab_fo_15m_cisd:" block, before the final
# "if st.session_state.get('scan_errors'):" line). Do not remove or
# reorder any existing "with tab_xxx:" blocks.
# ═══════════════════════════════════════════════════════════════════════

    # ── Order Block Scanner tab (new) ────────────────────────────────────
    with tab_order_block:
        st.caption(
            "F&O stocks only. Shows the most recent valid Bullish/Bearish Order Block per "
            "stock (Smart Money Concepts), with BOS/CHOCH structure, CISD confirmation, and "
            "Entry/SL/Targets. Signal Date/Time reflect the actual candle that FORMED the "
            "Order Block — never scan/system time — and stay fixed until a new Order Block forms."
        )
        ob_symbols = load_nse_fo_stock_symbols()
        st.caption(f"Loaded {len(ob_symbols)} F&O-permitted NSE stocks (indices excluded).")

        if not ob_symbols:
            st.warning("No F&O stock symbols loaded — check network access to public.fyers.in.")
        else:
            ob_limit = st.number_input(
                "Limit F&O symbols (0 = all)", min_value=0, max_value=len(ob_symbols),
                value=len(ob_symbols), step=25, key="ob_limit",
            )
            ob_universe = ob_symbols if ob_limit == 0 else ob_symbols[:ob_limit]

            if st.button(f"📦 Run Order Block Scan ({len(ob_universe)} symbols)", key="ob_run"):
                with st.spinner("Scanning F&O stocks for Order Blocks…"):
                    ob_results, ob_errors, ob_stats = run_order_block_scan(fyers, ob_universe)
                    ob_df = pd.DataFrame(ob_results)

                st.session_state["order_block_df"] = ob_df
                st.session_state["order_block_errors"] = ob_errors
                st.session_state["order_block_stats"] = ob_stats

            if "order_block_stats" in st.session_state:
                _display_scan_summary(st.session_state["order_block_stats"])

            ob_df = st.session_state.get("order_block_df")
            if ob_df is not None and not ob_df.empty:
                ob_sorted = ob_df.sort_values("Confidence %", ascending=False)
                st.dataframe(_style_dataframe(ob_sorted), use_container_width=True, height=500)
                st.download_button(
                    "📥 Download Order Block Signals as Excel",
                    data=to_excel_bytes(ob_sorted, "Order Blocks"),
                    file_name=f"nse_order_blocks_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_ob",
                )
            elif "order_block_df" in st.session_state:
                st.info("No valid Order Blocks found for this scan.")
            else:
                st.info("Run an Order Block scan above to see results here.")

            if st.session_state.get("order_block_errors"):
                with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['order_block_errors'])})"):
                    st.caption("Showing up to 20 — most stocks are simply skipped for missing/invalid data, not app errors.")
                    st.text("\n".join(st.session_state["order_block_errors"][:20]))
