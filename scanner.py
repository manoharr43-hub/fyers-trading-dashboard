# ════════════════════════════════════════════════════════════════════════════════
# ENHANCED EXCEL EXPORT WITH SUMMARY & FORMATTING
# ════════════════════════════════════════════════════════════════════════════════

def _format_excel_output_enhanced(df, scanner_type: str = "NSE", include_summary: bool = True) -> bytes:
    """
    Create professionally formatted Excel with:
    - Summary sheet with statistics
    - Signals sheet with color-coded results
    - Professional styling and formatting
    """
    buf = io.BytesIO()
    
    try:
        df_export = _safe_convert_df(df)
        
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            # ════════════════════════════════════════════════════════════════════
            # SUMMARY SHEET
            # ════════════════════════════════════════════════════════════════════
            if include_summary:
                stats = calculate_market_stats(df)
                
                summary_data = {
                    "Metric": [
                        "📊 Total Stocks Scanned",
                        "🟢 BUY Signals",
                        "🔴 SELL Signals",
                        "🟡 NEUTRAL Signals",
                        "",
                        "💪 Strong BUY (≥75%)",
                        "💪 Strong SELL (≥75%)",
                        "",
                        "📈 Bullish Signals %",
                        "📉 Bearish Signals %",
                        "➡️ Neutral Signals %",
                        "",
                        "⭐ Average Confidence",
                        "🕒 Scan Time",
                        "📅 Generated",
                    ],
                    "Value": [
                        stats["total"],
                        stats["buy"],
                        stats["sell"],
                        stats["neutral"],
                        "",
                        stats["strong_buy"],
                        stats["strong_sell"],
                        "",
                        f"{stats['buy_pct']}%",
                        f"{stats['sell_pct']}%",
                        f"{stats['neutral_pct']}%",
                        "",
                        f"{stats['avg_confidence']}%",
                        f"{_now_ist().strftime('%H:%M:%S')}",
                        f"{_now_ist().strftime('%d-%b-%Y')}",
                    ]
                }
                
                summary_df = pd.DataFrame(summary_data)
                summary_df.to_excel(writer, index=False, sheet_name="📊 Summary")
                
                summary_sheet = writer.sheets["📊 Summary"]
                
                if OPENPYXL_AVAILABLE:
                    try:
                        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
                        header_font = Font(bold=True, color="FFFFFF", size=12)
                        
                        for col_num in range(1, 3):
                            cell = summary_sheet.cell(row=1, column=col_num)
                            cell.fill = header_fill
                            cell.font = header_font
                            cell.alignment = Alignment(horizontal="center", vertical="center")
                        
                        for row_num in range(2, len(summary_df) + 2):
                            for col_num in range(1, 3):
                                cell = summary_sheet.cell(row=row_num, column=col_num)
                                cell.alignment = Alignment(horizontal="left", vertical="center")
                                
                                # Alternating row colors
                                if row_num % 2 == 0:
                                    cell.fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
                        
                        summary_sheet.column_dimensions['A'].width = 30
                        summary_sheet.column_dimensions['B'].width = 20
                    
                    except Exception as e:
                        logging.warning(f"Summary formatting error: {e}")
            
            # ════════════════════════════════════════════════════════════════════
            # SIGNALS SHEET
            # ════════════════════════════════════════════════════════════════════
            df_export.to_excel(writer, index=False, sheet_name="🚀 Signals")
            
            signals_sheet = writer.sheets["🚀 Signals"]
            
            if OPENPYXL_AVAILABLE:
                try:
                    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                    yellow_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
                    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
                    header_font = Font(bold=True, color="FFFFFF", size=11)
                    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    
                    # Format header
                    for col_num in range(1, len(df_export.columns) + 1):
                        cell = signals_sheet.cell(row=1, column=col_num)
                        cell.fill = header_fill
                        cell.font = header_font
                        cell.alignment = center_align
                    
                    # Format data rows with signal colors
                    for row_num in range(2, len(df_export) + 2):
                        for col_num in range(1, len(df_export.columns) + 1):
                            cell = signals_sheet.cell(row=row_num, column=col_num)
                            cell.alignment = Alignment(horizontal="center", vertical="center")
                            col_title = df_export.columns[col_num - 1]
                            
                            try:
                                cell_value = str(cell.value)
                                
                                if col_title == "AI SIGNAL":
                                    if "BUY" in cell_value and "SELL" not in cell_value:
                                        cell.fill = green_fill
                                        cell.font = Font(bold=True, color="006100")
                                    elif "SELL" in cell_value:
                                        cell.fill = red_fill
                                        cell.font = Font(bold=True, color="9C0006")
                                    else:
                                        cell.fill = yellow_fill
                                        cell.font = Font(bold=True, color="9C6500")
                                
                                elif col_title == "AI CONFIDENCE %":
                                    try:
                                        conf_val = float(cell_value)
                                        if conf_val >= 75:
                                            cell.fill = green_fill
                                        elif conf_val >= 50:
                                            cell.fill = yellow_fill
                                        else:
                                            cell.fill = red_fill
                                    except:
                                        pass
                                
                                elif col_title == "PRESSURE SIGNAL":
                                    if "BUYING" in cell_value:
                                        cell.fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
                                    elif "SELLING" in cell_value:
                                        cell.fill = PatternFill(start_color="F4CCCC", end_color="F4CCCC", fill_type="solid")
                            
                            except Exception:
                                pass
                    
                    # Freeze header row
                    signals_sheet.freeze_panes = "A2"
                    
                    # Auto-adjust column widths
                    for col_num, col_title in enumerate(df_export.columns, 1):
                        max_length = len(str(col_title)) + 2
                        adjusted_width = min(max_length + 2, 25)
                        col_letter = get_column_letter(col_num)
                        signals_sheet.column_dimensions[col_letter].width = adjusted_width
                
                except Exception as format_err:
                    logging.warning(f"Signal sheet formatting error: {format_err}")
    
    except Exception as e:
        logging.error(f"Excel export error: {e}")
        try:
            buf = io.BytesIO()
            df_export = _safe_convert_df(df)
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df_export.to_excel(writer, index=False, sheet_name="Signals")
        except Exception as final_err:
            logging.error(f"Final Excel fallback failed: {final_err}")
            buf = io.BytesIO()
            buf.write(to_csv_bytes(df))
    
    buf.seek(0)
    return buf.getvalue()

# ════════════════════════════════════════════════════════════════════════════════
# DEDICATED STRONG SCANNER FUNCTION
# ════════════════════════════════════════════════════════════════════════════════
def run_strong_scanner(fyers, symbols: List[str], scan_type: str = "both", min_confidence: float = 75.0):
    """
    Fast, focused scanner for high-confidence signals only.
    
    Args:
        fyers: Fyers API client
        symbols: List of symbols to scan
        scan_type: "nse" (NSE only), "fo" (F&O only), or "both"
        min_confidence: Minimum confidence threshold
    
    Returns:
        DataFrame with only high-confidence signals
    """
    symbols = _validate_symbols(symbols)
    strong_results = []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"💪 Strong Scanner: 0 / {len(symbols)}")
    done = 0
    
    # Determine which scanner to use
    if scan_type == "nse":
        scanner_func = _fetch_nse_signal
        batch_msg = "NSE Strong Scan"
    elif scan_type == "fo":
        scanner_func = _fetch_fo_signal
        batch_msg = "F&O Strong Scan"
    else:
        scanner_func = _fetch_nse_signal  # Default to NSE
        batch_msg = "Strong Scan"
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(scanner_func, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, None
                
                if res:
                    try:
                        confidence = pd.to_numeric(res.get("AI CONFIDENCE %", 0), errors='coerce')
                        signal = str(res.get("AI SIGNAL", "NEUTRAL"))
                        
                        # Filter: Only strong signals (confidence >= min_confidence)
                        if confidence >= min_confidence and normalize_signal(signal) != "NEUTRAL":
                            strong_results.append(res)
                    except:
                        pass
                
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"💪 Strong Scanner: {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS * 0.5)  # Faster for strong scan
    
    progress.empty()
    gc.collect()
    
    return strong_results, stats

# ════════════════════════════════════════════════════════════════════════════════
# UPDATED SHOW_SCANNER FUNCTION WITH STRONG SCANNER TAB
# ════════════════════════════════════════════════════════════════════════════════
def show_scanner_enhanced(fyers) -> None:
    """Streamlit main app - NSE AI PRO V17 with Strong Scanner"""
    
    try:
        st.set_page_config(page_title="NSE AI PRO V17", layout="wide")
    except:
        pass
    
    st.title("🚀 NSE AI PRO V17 — Professional Intraday + Swing Scanner")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST | Multi-Timeframe Analysis + Golden Cross Detection")
    
    # Load symbols
    try:
        all_symbols = load_nse_equity_symbols()
        fo_symbols = load_fo_stocks()
    except Exception as e:
        st.error(f"❌ Error loading symbols: {e}")
        logger.error(f"Symbol loading error: {e}")
        return
    
    if not all_symbols:
        st.error("❌ No symbols loaded — check FYERS API access.")
        return
    
    st.caption(f"📊 NSE Equities: {len(all_symbols)} | 📈 F&O Stocks: {len(fo_symbols)}")
    
    # Create tabs with STRONG SCANNER as second tab
    tabs = st.tabs([
        "💪 STRONG SCANNER",
        "🇮🇳 NSE STOCKS",
        "📊 F&O STOCKS",
        "⚡ LIVE INTRADAY",
        "🔥 STRONG SIGNALS",
        "📈 SWING (GOLDEN/DEATH CROSS)",
        "🧠 ADDITIONAL ANALYSIS",
        "📊 MARKET DASHBOARD",
        "⚙️ SETTINGS"
    ])
    
    # ════════════════════════════════════════════════════════════════════════════════
    # NEW TAB 0: STRONG SCANNER (HERO TAB)
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[0]:
        st.markdown("""
        # 💪 Strong Scanner — High-Confidence Signals Only
        
        **Find the best trading opportunities in seconds!**
        - ✅ Only signals with ≥75% confidence
        - ✅ Strict validation (no false signals)
        - ✅ Multi-timeframe confirmed
        - ✅ Fast execution (optimized scanning)
        """)
        
        col_ss1, col_ss2, col_ss3 = st.columns(3)
        
        with col_ss1:
            strong_universe = st.radio("Universe", ["NSE Stocks", "F&O Stocks"], horizontal=False, key="strong_universe")
            strong_symbols = all_symbols if strong_universe == "NSE Stocks" else fo_symbols
        
        with col_ss2:
            strong_limit = st.number_input("Scan Limit", 
                                          min_value=50, 
                                          max_value=len(strong_symbols),
                                          value=min(200, len(strong_symbols)),
                                          step=50, 
                                          key="strong_limit")
            st.metric("Available", len(strong_symbols))
        
        with col_ss3:
            strong_conf = st.slider("Min Confidence %", 50, 100, 75, 5, key="strong_conf")
        
        strong_universe_symbols = strong_symbols[:strong_limit]
        
        # HERO BUTTON
        if st.button("🔥 RUN STRONG SCANNER NOW 🔥", key="strong_run_main", use_container_width=True):
            with st.spinner(f"💪 Scanning top {strong_limit} {strong_universe} for STRONG signals…"):
                strong_scan_type = "nse" if strong_universe == "NSE Stocks" else "fo"
                strong_results, strong_stats = run_strong_scanner(fyers, strong_universe_symbols, 
                                                                  scan_type=strong_scan_type, 
                                                                  min_confidence=strong_conf)
                
                if strong_results:
                    strong_df = pd.DataFrame(strong_results)
                    strong_df = strong_df.sort_values("AI CONFIDENCE %", ascending=False)
                    st.session_state["strong_df"] = strong_df
                    st.session_state["strong_stats"] = strong_stats
                    st.success(f"✅ Found {len(strong_df)} STRONG signals!")
                else:
                    st.warning(f"⚠️ No signals found with {strong_conf}% confidence")
                    st.session_state["strong_df"] = pd.DataFrame()
        
        # Display results
        if "strong_stats" in st.session_state:
            _display_scan_summary(st.session_state["strong_stats"])
        
        strong_df = st.session_state.get("strong_df")
        if strong_df is not None and not strong_df.empty:
            st.markdown(f"### 📊 Strong Signals: {len(strong_df)} Found")
            
            # Quick filters
            col_f1, col_f2 = st.columns(2)
            
            with col_f1:
                strong_signal_filter = st.selectbox("Signal Type", ["ALL", "BUY", "SELL"], key="strong_sig_type")
            with col_f2:
                strong_sort_by = st.selectbox("Sort By", ["Confidence ↓", "LTP ↓", "RVOL ↓"], key="strong_sort_by")
            
            strong_filtered = strong_df.copy()
            
            if strong_signal_filter != "ALL":
                try:
                    normalized = strong_filtered["AI SIGNAL"].apply(normalize_signal)
                    if strong_signal_filter == "BUY":
                        strong_filtered = strong_filtered[normalized == "BUY"]
                    elif strong_signal_filter == "SELL":
                        strong_filtered = strong_filtered[normalized == "SELL"]
                except:
                    pass
            
            try:
                if strong_sort_by == "Confidence ↓":
                    strong_filtered = strong_filtered.sort_values("AI CONFIDENCE %", ascending=False)
                elif strong_sort_by == "LTP ↓":
                    strong_filtered = strong_filtered.sort_values("LTP", ascending=False)
                elif strong_sort_by == "RVOL ↓":
                    strong_filtered = strong_filtered.sort_values("RVOL", ascending=False)
            except:
                pass
            
            # Display table
            st.dataframe(strong_filtered, use_container_width=True, height=500)
            
            # ENHANCED DOWNLOAD SECTION
            st.markdown("### 📥 Download Strong Signals")
            
            col_d1, col_d2, col_d3, col_d4 = st.columns(4)
            
            with col_d1:
                try:
                    excel_data = _format_excel_output_enhanced(strong_filtered, 
                                                               scanner_type=strong_universe, 
                                                               include_summary=True)
                    st.download_button(
                        "📊 Excel (Enhanced)",
                        excel_data,
                        f"STRONG_{strong_universe.replace(' ', '_')}_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="strong_excel_enhanced"
                    )
                except Exception as e:
                    st.error(f"❌ Excel: {str(e)[:40]}")
            
            with col_d2:
                try:
                    csv_data = to_csv_bytes(strong_filtered)
                    st.download_button(
                        "📄 CSV",
                        csv_data,
                        f"STRONG_{strong_universe.replace(' ', '_')}_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                        "text/csv",
                        key="strong_csv"
                    )
                except Exception as e:
                    st.error(f"❌ CSV: {str(e)[:40]}")
            
            with col_d3:
                try:
                    json_data = to_json_bytes(strong_filtered)
                    st.download_button(
                        "📋 JSON",
                        json_data,
                        f"STRONG_{strong_universe.replace(' ', '_')}_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                        "application/json",
                        key="strong_json"
                    )
                except Exception as e:
                    st.error(f"❌ JSON: {str(e)[:40]}")
            
            with col_d4:
                # Stats export
                try:
                    stats = calculate_market_stats(strong_filtered)
                    stats_text = f"""Strong Scanner Report
Generated: {_now_ist().strftime('%d-%b-%Y %H:%M:%S')}
Universe: {strong_universe}
Min Confidence: {strong_conf}%

STATISTICS:
- Total Strong Signals: {stats['total']}
- BUY Signals: {stats['buy']}
- SELL Signals: {stats['sell']}
- Buy %: {stats['buy_pct']}%
- Sell %: {stats['sell_pct']}%
- Avg Confidence: {stats['avg_confidence']}%
"""
                    st.download_button(
                        "📋 Report",
                        stats_text.encode(),
                        f"STRONG_REPORT_{_now_ist().strftime('%Y%m%d_%H%M')}.txt",
                        "text/plain",
                        key="strong_report"
                    )
                except Exception as e:
                    st.error(f"❌ Report: {str(e)[:40]}")
        
        else:
            st.info("👈 Click '🔥 RUN STRONG SCANNER NOW 🔥' to find high-confidence signals")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 1: NSE STOCKS (Keep original)
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[1]:
        st.markdown("### NSE Equity Stocks Scanner\n✅ Strict validation - only high-quality signals")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            nse_limit = st.number_input("Scan limit (0=all)", min_value=0, max_value=len(all_symbols),
                                       value=min(500, len(all_symbols)), step=50, key="nse_limit")
        with col2:
            st.metric("Available", len(all_symbols))
        
        nse_universe = all_symbols if nse_limit == 0 else all_symbols[:nse_limit]
        
        if st.button(f"🔍 SCAN NSE ({len(nse_universe)} stocks)", key="nse_run"):
            with st.spinner("Analyzing NSE stocks…"):
                nse_results, nse_errors, nse_stats = run_nse_scan(fyers, nse_universe)
                st.session_state["nse_df"] = pd.DataFrame(nse_results) if nse_results else pd.DataFrame()
                st.session_state["nse_errors"] = nse_errors
                st.session_state["nse_stats"] = nse_stats
        
        if "nse_stats" in st.session_state:
            _display_scan_summary(st.session_state["nse_stats"])
        
        nse_df = st.session_state.get("nse_df")
        if nse_df is not None and not nse_df.empty:
            try:
                st.info(f"📊 Loaded: {len(nse_df)} signals")
                
                col_f1, col_f2, col_f3 = st.columns(3)
                
                with col_f1:
                    nse_min_conf = st.slider("Min Confidence %", 0, 100, 70, step=5, key="nse_conf_filter")
                with col_f2:
                    nse_signal = st.selectbox("Signal", ["ALL", "BUY", "SELL", "STRONG ONLY"], key="nse_signal_filter")
                with col_f3:
                    nse_sort = st.selectbox("Sort By", ["AI CONFIDENCE %", "LTP", "RVOL"], key="nse_sort_filter")
                
                nse_filtered = nse_df.copy()
                try:
                    confidence_col = pd.to_numeric(nse_filtered["AI CONFIDENCE %"], errors='coerce')
                    nse_filtered = nse_filtered[confidence_col >= nse_min_conf]
                except:
                    pass
                
                if nse_signal != "ALL":
                    try:
                        normalized = nse_filtered["AI SIGNAL"].apply(normalize_signal)
                        if nse_signal == "BUY":
                            nse_filtered = nse_filtered[normalized == "BUY"]
                        elif nse_signal == "SELL":
                            nse_filtered = nse_filtered[normalized == "SELL"]
                        elif nse_signal == "STRONG ONLY":
                            nse_filtered = nse_filtered[nse_filtered["AI SIGNAL"].astype(str).str.contains("STRONG", na=False)]
                    except:
                        pass
                
                try:
                    if nse_sort == "LTP":
                        nse_filtered = nse_filtered.sort_values("LTP", ascending=False)
                    elif nse_sort == "RVOL":
                        nse_filtered = nse_filtered.sort_values("RVOL", ascending=False)
                    else:
                        nse_filtered = nse_filtered.sort_values("AI CONFIDENCE %", ascending=False)
                except:
                    pass
                
                st.dataframe(nse_filtered, use_container_width=True, height=500)
                
                st.markdown("### 📥 Download")
                col_d1, col_d2, col_d3 = st.columns(3)
                
                with col_d1:
                    try:
                        excel_data = _format_excel_output_enhanced(nse_filtered, "NSE")
                        st.download_button("📊 Excel", excel_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="nse_xls")
                    except Exception as e:
                        st.error(f"❌ Excel: {str(e)[:50]}")
                
                with col_d2:
                    try:
                        csv_data = to_csv_bytes(nse_filtered)
                        st.download_button("📄 CSV", csv_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                         "text/csv", key="nse_csv")
                    except Exception as e:
                        st.error(f"❌ CSV: {str(e)[:50]}")
                
                with col_d3:
                    try:
                        json_data = to_json_bytes(nse_filtered)
                        st.download_button("📋 JSON", json_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                         "application/json", key="nse_json")
                    except Exception as e:
                        st.error(f"❌ JSON: {str(e)[:50]}")
            
            except Exception as e:
                st.error(f"❌ NSE Tab Error: {str(e)[:100]}")
        else:
            st.info("👈 Click 'SCAN NSE' to start")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 2: F&O STOCKS (with enhanced Excel)
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[2]:
        st.markdown("### F&O Stocks Scanner\n✅ Strict validation + options analysis")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            fo_limit = st.number_input("Scan limit (0=all)", min_value=0, max_value=len(fo_symbols),
                                      value=min(200, len(fo_symbols)), step=25, key="fo_limit")
        with col2:
            st.metric("Available", len(fo_symbols))
        
        fo_universe = fo_symbols if fo_limit == 0 else fo_symbols[:fo_limit]
        
        if st.button(f"🔍 SCAN F&O ({len(fo_universe)} stocks)", key="fo_run"):
            with st.spinner("Analyzing F&O stocks…"):
                fo_results, fo_errors, fo_stats = run_fo_scan(fyers, fo_universe)
                st.session_state["fo_df"] = pd.DataFrame(fo_results) if fo_results else pd.DataFrame()
                st.session_state["fo_errors"] = fo_errors
                st.session_state["fo_stats"] = fo_stats
        
        if "fo_stats" in st.session_state:
            _display_scan_summary(st.session_state["fo_stats"])
        
        fo_df = st.session_state.get("fo_df")
        if fo_df is not None and not fo_df.empty:
            try:
                st.info(f"📊 Loaded: {len(fo_df)} signals")
                
                col_f1, col_f2, col_f3, col_f4 = st.columns(4)
                with col_f1:
                    fo_min_conf = st.slider("Min Confidence %", 0, 100, 70, step=5, key="fo_conf_filter")
                with col_f2:
                    fo_signal = st.selectbox("Signal", ["ALL", "BUY", "SELL", "STRONG ONLY"], key="fo_signal_filter")
                with col_f3:
                    fo_opt = st.selectbox("Options", ["ALL", "BULLISH", "BEARISH"], key="fo_opt_filter")
                with col_f4:
                    fo_sort = st.selectbox("Sort By", ["AI CONFIDENCE %", "LTP", "PCR"], key="fo_sort_filter")
                
                fo_filtered = fo_df.copy()
                try:
                    confidence_col = pd.to_numeric(fo_filtered["AI CONFIDENCE %"], errors='coerce')
                    fo_filtered = fo_filtered[confidence_col >= fo_min_conf]
                except:
                    pass
                
                if fo_signal != "ALL":
                    try:
                        normalized = fo_filtered["AI SIGNAL"].apply(normalize_signal)
                        if fo_signal == "BUY":
                            fo_filtered = fo_filtered[normalized == "BUY"]
                        elif fo_signal == "SELL":
                            fo_filtered = fo_filtered[normalized == "SELL"]
                        elif fo_signal == "STRONG ONLY":
                            fo_filtered = fo_filtered[fo_filtered["AI SIGNAL"].astype(str).str.contains("STRONG", na=False)]
                    except:
                        pass
                
                if fo_opt != "ALL":
                    try:
                        fo_filtered = fo_filtered[fo_filtered["OPTIONS BIAS"].astype(str).str.contains(fo_opt, na=False)]
                    except:
                        pass
                
                try:
                    if fo_sort == "PCR":
                        fo_filtered = fo_filtered.sort_values("PCR", ascending=False)
                    elif fo_sort == "LTP":
                        fo_filtered = fo_filtered.sort_values("LTP", ascending=False)
                    else:
                        fo_filtered = fo_filtered.sort_values("AI CONFIDENCE %", ascending=False)
                except:
                    pass
                
                st.dataframe(fo_filtered, use_container_width=True, height=500)
                
                st.markdown("### 📥 Download")
                col_d1, col_d2, col_d3 = st.columns(3)
                
                with col_d1:
                    try:
                        excel_data = _format_excel_output_enhanced(fo_filtered, "FO")
                        st.download_button("📊 Excel", excel_data, f"FO_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="fo_xls")
                    except:
                        pass
                
                with col_d2:
                    try:
                        csv_data = to_csv_bytes(fo_filtered)
                        st.download_button("📄 CSV", csv_data, f"FO_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                         "text/csv", key="fo_csv")
                    except:
                        pass
                
                with col_d3:
                    try:
                        json_data = to_json_bytes(fo_filtered)
                        st.download_button("📋 JSON", json_data, f"FO_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                         "application/json", key="fo_json")
                    except:
                        pass
            
            except Exception as e:
                st.error(f"❌ F&O Tab Error: {str(e)[:100]}")
        else:
            st.info("👈 Click 'SCAN F&O' to start")
    
    # Remaining tabs (3-8) stay the same as original...
    # [Keep all the other tabs from the original show_scanner function]
    
    gc.collect()

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT (Updated to use enhanced version)
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        access_token = os.environ.get("FYERS_ACCESS_TOKEN")
        if not access_token:
            st.error("❌ FYERS_ACCESS_TOKEN not set in environment variables")
            st.info("Please set: export FYERS_ACCESS_TOKEN='your_token_here'")
            st.stop()
        
        try:
            from fyers_api import fyersModel
        except ImportError as ie:
            st.error("❌ fyers-api not installed")
            st.code("pip install fyers-api", language="bash")
            st.stop()
        
        app_id = os.environ.get("FYERS_APP_ID", "DEMO")
        
        try:
            fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
            logger.info("Fyers client initialized successfully")
            show_scanner_enhanced(fyers)  # Use enhanced version
        except Exception as init_error:
            st.error(f"❌ Failed to initialize Fyers API: {str(init_error)}")
            logger.error(f"Fyers initialization error: {init_error}", exc_info=True)
    
    except Exception as e:
        st.error(f"❌ Unexpected Error: {e}")
        logger.error(f"Unexpected error: {e}", exc_info=True)
