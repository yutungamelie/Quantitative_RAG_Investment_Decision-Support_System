import pandas as pd
import streamlit as st

from data.tickers import TAIWAN_STOCKS
from database.chat_history import add_message, clear_messages
from decision.final_decision_chain import generate_final_decision
from exports.export_utils import build_export_payload, payload_to_json_string


st.set_page_config(
    page_title="Quantitative RAG Investment Decision-Support System",
    page_icon="$",
    layout="wide",
)

st.title("Quantitative RAG Investment Decision-Support System")
st.caption("台股量化 RAG 投資決策輔助系統")
st.write(
    "設定資金與股票池，檢視量化評分、資金配置與分析來源，並匯出本次討論紀錄。"
)

st.warning(
    "提醒：本工具僅供學習與決策輔助，不保證獲利，也不構成正式投資建議。"
)


if "messages" not in st.session_state:
    st.session_state["messages"] = []

if "latest_final_decision" not in st.session_state:
    st.session_state["latest_final_decision"] = None

if "latest_candidates" not in st.session_state:
    st.session_state["latest_candidates"] = []


def candidates_to_dataframe(candidates: list[dict]) -> pd.DataFrame:
    if not candidates:
        return pd.DataFrame()

    rows = []

    for item in candidates:
        rows.append({
            "股票代號": item["ticker"],
            "股票名稱": item["name"],
            "類型": item["type"],
            "產業": item["industry"],
            "收盤價": item["close"],
            "MA20": item["ma20"],
            "MA60": item["ma60"],
            "RSI14": item["rsi14"],
            "20日報酬率%": item["return_20d"],
            "20日波動率%": item["volatility_20d"],
            "趨勢分數": item["trend_score"],
            "動能分數": item["momentum_score"],
            "風險分數": item["risk_score"],
            "總分": item["total_score"],
            "初步決策": item["action"],
        })

    return pd.DataFrame(rows)


def position_plan_to_dataframe(plan: list[dict]) -> pd.DataFrame:
    if not plan:
        return pd.DataFrame()

    rows = []

    for item in plan:
        rows.append({
            "股票代號": item["ticker"],
            "股票名稱": item["name"],
            "類型": item["type"],
            "收盤價": item["close"],
            "量化總分": item["total_score"],
            "初步決策": item["action"],
            "建議比例%": item["weight"],
            "建議金額": item["suggested_amount"],
            "估計股數": item["estimated_shares"],
        })

    return pd.DataFrame(rows)


with st.sidebar:
    st.header("使用方式")
    st.write("1. 設定可用資金與股票池")
    st.write("2. 輸入問題，產生分析")
    st.write("3. 檢視評分、配置與參考資料")
    st.write("4. 追問或匯出 JSON 紀錄")

    st.divider()

    st.header("投資設定")

    cash = st.number_input(
        "可用資金",
        min_value=1000,
        max_value=10000000,
        value=70224,
        step=1000,
    )

    default_pool = ["0050", "006208", "00878", "00919", "2330"]

    stock_pool = st.multiselect(
        "選擇股票池",
        options=list(TAIWAN_STOCKS.keys()),
        default=[ticker for ticker in default_pool if ticker in TAIWAN_STOCKS],
        format_func=lambda x: f"{x} {TAIWAN_STOCKS[x]['name']}",
    )

    user_question = st.text_area(
        "輸入投資問題",
        value="我現在有 70,224 元，今天適合買什麼？",
        height=120,
    )

    if st.button("產生投資建議"):
        try:
            st.session_state["messages"] = add_message(
                st.session_state["messages"],
                role="user",
                content=user_question,
            )

            with st.spinner("正在計算量化分數、檢索 RAG 規則並產生建議..."):
                result = generate_final_decision(
                    user_question=user_question,
                    cash=int(cash),
                    stock_pool=stock_pool,
                )

            st.session_state["latest_final_decision"] = result
            st.session_state["latest_candidates"] = result.get("candidate_list", [])

            st.session_state["messages"] = add_message(
                st.session_state["messages"],
                role="assistant",
                content=result["answer"],
                references=result.get("references", []),
            )

        except Exception as e:
            st.error(f"產生投資建議失敗：{e}")

    if st.button("清除對話紀錄"):
        st.session_state["messages"] = clear_messages()
        st.session_state["latest_final_decision"] = None
        st.session_state["latest_candidates"] = []
        st.rerun()

    st.divider()

    st.header("匯出資料")

    export_payload = build_export_payload(
        cash=int(cash),
        stock_pool=stock_pool,
        messages=st.session_state["messages"],
        latest_final_decision=st.session_state["latest_final_decision"],
        latest_candidates=st.session_state["latest_candidates"],
    )

    st.download_button(
        label="匯出 JSON",
        data=payload_to_json_string(export_payload),
        file_name="investment_chat_export.json",
        mime="application/json",
    )


latest_decision = st.session_state["latest_final_decision"]

if latest_decision:
    st.subheader("股票評分表")

    candidates_df = candidates_to_dataframe(
        latest_decision.get("candidate_list", [])
    )

    if not candidates_df.empty:
        st.dataframe(
            candidates_df,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("目前尚無股票評分資料。")

    st.subheader("建議資金配置")

    position_df = position_plan_to_dataframe(
        latest_decision.get("position_plan_list", [])
    )

    if not position_df.empty:
        st.dataframe(
            position_df,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("目前沒有可配置標的，建議保留現金並觀望。")

    st.subheader("最終投資建議")

    if latest_decision["success"]:
        st.success("已成功產生完整投資建議。")
    else:
        st.warning("Gemini 暫時無法產生完整建議，但已保留量化資料與 RAG 引用資料。")

    st.write(latest_decision["answer"])

    st.subheader("RAG 引用資料")

    references = latest_decision.get("references", [])

    if references:
        for i, ref in enumerate(references, start=1):
            with st.expander(f"引用資料 {i}：{ref.get('filename', 'unknown')}"):
                st.write(ref.get("content", ""))
    else:
        st.info("目前沒有引用資料。")

else:
    st.info("請先在左側輸入資金、選擇股票池，然後按「產生投資建議」。")


st.divider()
st.subheader("問答區與對話紀錄")

for message in st.session_state["messages"]:
    with st.chat_message(message["role"]):
        st.write(message["content"])

        refs = message.get("references", [])
        if refs:
            with st.expander("查看本次回答引用資料"):
                for i, ref in enumerate(refs, start=1):
                    st.markdown(f"### 引用資料 {i}：{ref.get('filename', 'unknown')}")
                    st.write(ref.get("content", ""))


chat_question = st.chat_input("請輸入你的追問，例如：為什麼不要一次買滿？")

if chat_question:
    st.session_state["messages"] = add_message(
        st.session_state["messages"],
        role="user",
        content=chat_question,
    )

    try:
        with st.chat_message("user"):
            st.write(chat_question)

        with st.chat_message("assistant"):
            with st.spinner("投資助理思考中..."):
                result = generate_final_decision(
                    user_question=chat_question,
                    cash=int(cash),
                    stock_pool=stock_pool,
                )

                answer = result["answer"]
                references = result.get("references", [])

                st.write(answer)

                if references:
                    with st.expander("查看本次回答引用資料"):
                        for i, ref in enumerate(references, start=1):
                            st.markdown(f"### 引用資料 {i}：{ref.get('filename', 'unknown')}")
                            st.write(ref.get("content", ""))

        st.session_state["messages"] = add_message(
            st.session_state["messages"],
            role="assistant",
            content=answer,
            references=references,
        )

        st.session_state["latest_final_decision"] = result
        st.session_state["latest_candidates"] = result.get("candidate_list", [])

    except Exception as e:
        error_message = f"系統發生錯誤：{e}"

        with st.chat_message("assistant"):
            st.write(error_message)

        st.session_state["messages"] = add_message(
            st.session_state["messages"],
            role="assistant",
            content=error_message,
        )
