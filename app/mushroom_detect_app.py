import streamlit as st
from ultralytics import YOLO
from PIL import Image
import cv2
import numpy as np
import os
import csv
import re
from datetime import datetime
import sys
import signal
from PIL import ImageDraw, ImageFont

# 1. 가상환경의 패키지 저장소 절대 경로 계산
base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
site_packages_path = os.path.join(base_path, "py310", "Lib", "site-packages")

# 2. 파이썬 경로 리스트 맨 앞에 추가 (우선순위 1위)
if site_packages_path not in sys.path:
    sys.path.insert(0, site_packages_path)

# 3. 디버깅 출력 (나중에 확인용)
print(f"Searching in: {site_packages_path}")

# LLM/RAG 패키지는 선택 사항: 없어도 스마트팜 대시보드와 YOLO 판독은 동작
try:
    from llama_cpp import Llama
except ImportError as e:
    print(f"Import Error details: {e}")
    Llama = None
try:
    from sentence_transformers import SentenceTransformer
    from sklearn.metrics.pairwise import cosine_similarity
except ImportError as e:
    print(f"Import Error details: {e}")
    SentenceTransformer = None

# 스마트팜(멀티 컨테이너·비닐하우스) 모듈
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smartfarm_ui import get_farm_manager, render_dashboard, render_farm_detail, render_farm_admin
from smartfarm import FARM_TYPES

st.set_page_config(page_title="표고버섯 AI 스마트팜", page_icon="🍄", layout="wide")

# --- [1. 세션 상태 초기화] ---
if 'run_cam' not in st.session_state:
    st.session_state.run_cam = False
if 'last_ai_res' not in st.session_state:
    st.session_state.last_ai_res = "분석 대기 중..."
if 'prev_status' not in st.session_state:
    st.session_state.prev_status = ""


# --- [2. CSV 저장 (한글 깨짐 방지)] ---
def save_feedback(status, context, answer, score):
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mushroom_ai_feedback.csv')
    file_exists = os.path.isfile(log_file)
    with open(log_file, mode='a', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['일시', '진단상태', '참고문헌', 'AI답변', '사용자평가'])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status, context, answer, score])

# --- [3. AI 답변 정제 함수] ---
def clean_ai_answer(text):
    text = text.replace("**", "").replace("#", "").strip()
    # 영문 찌꺼기 제거 및 정리
    text = re.sub(r'[a-zA-Z]{10,}', '', text) 
    return text

# --- [4. 모델 로드] ---
@st.cache_resource
def init_all_models():
    # 1. 경로 설정 (배치 파일 실행 환경 고려)
    curr_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 2. YOLO 모델 로드
    yolo_path = os.path.join(curr_dir, 'best.pt')
    yolo = YOLO(yolo_path)    

    # 3. 임베딩 모델 및 매뉴얼 로드 (RAG 설정)
    embed_model = None
    chunks = []
    embeddings = []
    
    try:
        if SentenceTransformer is None:
            raise ImportError("sentence-transformers 미설치")
        # 한국어 성능이 좋은 모델로 명시적 생성
        embed_model = SentenceTransformer('jhgan/ko-sroberta-multitask')
        
        manual_path = os.path.join(curr_dir, 'mushroom_manual.txt')
        if os.path.exists(manual_path):
            with open(manual_path, 'r', encoding='utf-8') as f:
                content = f.read()
                # 간단한 청킹 (문단 단위)
                chunks = [p.strip() for p in content.split('\n\n') if p.strip()]
                embeddings = embed_model.encode(chunks)
        else:
            st.error(f"매뉴얼 파일을 찾을 수 없습니다: {manual_path}")
    except Exception as e:
        st.warning(f"임베딩 모델 로드 중 오류 발생: {e}")

    # 4. LLM(Gemma) 모델 로드
    llm = None
    try:
        if Llama is None:
            raise ImportError("llama-cpp-python 미설치")
        gguf_files = [f for f in os.listdir(curr_dir) if f.endswith('.gguf')]
        if gguf_files:
            # 가장 용량이 큰 GGUF 파일을 자동으로 선택
            model_path = os.path.join(curr_dir, max(gguf_files, key=lambda f: os.path.getsize(os.path.join(curr_dir, f))))
            llm = Llama(model_path=model_path, n_ctx=512)
        else:
            st.error("GGUF 모델 파일이 app 폴더에 없습니다.")
    except Exception as e:
        st.error(f"LLM 로드 실패: {e}")

    # 모든 변수가 값이 있든 None이든 반환되도록 보장
    return yolo, embed_model, chunks, embeddings, llm


disease_lookup = {
    "dis": "푸른곰팡이병", 
    "mold": "털곰팡이병/흰곰팡이병", 
    "spot": "세균성 갈색무늬병"
}

def search_rag(query, k=3):
    if not chunks or embed_model is None: return "매뉴얼을 참고하세요."
    query_vec = embed_model.encode([query])
    distances = cosine_similarity(query_vec, embeddings)[0]
    top_indices = distances.argsort()[-k:][::-1]
    return "\n".join([chunks[i] for i in top_indices])

# --- [5. UI 스타일] ---
def apply_custom_css(theme):
    bg, txt, c_bg = ("#ffffff", "#000000", "#f8f9fa") if theme == "Light (화이트)" else ("#0e1117", "#ffffff", "#262936")
    st.markdown(f"""<style>
        .stApp {{ background-color: {bg}; color: {txt}; }}
        .main-header {{ background: {c_bg}; padding: 15px; border-radius: 20px; text-align: center; margin-bottom: 25px; border: 1px solid #ddd; }}
        .ai-box {{ background: {c_bg}; border-left: 5px solid #4CAF50; padding: 20px; border-radius: 12px; line-height: 1.8; }}
        .result-card {{ background: {c_bg}; padding: 15px; border-radius: 10px; border: 1px solid #ddd; margin-bottom: 12px; }}
        .loading-text {{ color: #FF9800; font-weight: bold; font-size: 1.1rem; margin: 15px 0; animation: blink 1.5s infinite; }}
        @keyframes blink {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} 100% {{ opacity: 1; }} }}
    </style>""", unsafe_allow_html=True)

with st.sidebar:
    st.title("🍄 설 정")
    theme = st.radio("🎨 테마", ["Light (화이트)", "Dark (블랙)"])
    mode = st.selectbox("🖥️ 메뉴", ["🏠 통합 대시보드", "🌡️ 농장 모니터링·제어", "📸 사진 분석",
                                     "📹 실시간 영상", "⚙️ 농장 관리"])

    farm_mgr = get_farm_manager()
    farm_ids = [f["id"] for f in farm_mgr.farms]
    farm_names = {f["id"]: f"{FARM_TYPES.get(f.get('type'), ' ').split()[0]} {f['name']}" for f in farm_mgr.farms}
    current_farm = st.selectbox("🏭 대상 농장", farm_ids, format_func=lambda x: farm_names.get(x, x)) if farm_ids else None

    if mode in ("📸 사진 분석", "📹 실시간 영상"):
        conf_v = st.slider("🎯 탐지 민감도", 0.1, 1.0, 0.3)
        iou_v = st.slider("중복제거", 0.1, 0.9, 0.35)

apply_custom_css(theme)
st.markdown('<div class="main-header"><h1>🍄 표고버섯 AI 스마트팜</h1></div>', unsafe_allow_html=True)

if mode in ("📸 사진 분석", "📹 실시간 영상"):
    yolo_model, embed_model, chunks, embeddings, llm_model = init_all_models()

# 현재 농장 환경을 AI 처방에 함께 제공
env_context = farm_mgr.env_summary(current_farm) if current_farm else ""

disease_lookup = {"dis": "푸른곰팡이병", "mold": "털곰팡이병", "spot": "갈색점무늬병"}

# --- [6. 분석 로직] ---

if mode == "🏠 통합 대시보드":
    render_dashboard(farm_mgr)

elif mode == "🌡️ 농장 모니터링·제어":
    render_farm_detail(farm_mgr, current_farm)

elif mode == "⚙️ 농장 관리":
    render_farm_admin(farm_mgr)

elif mode == "📸 사진 분석":
    col1, col2 = st.columns([1.2, 1])
    boxes = []
    
    with col1:
        f = st.file_uploader("이미지 업로드", type=["jpg", "png", "jpeg"])
        if f:
            img = Image.open(f).convert("RGB") # 형식 통일
            # [수정 1] classes 옵션을 제거하여 모든 학습 객체(배지 포함)를 탐지하도록 함
            res = yolo_model.predict(img, conf=conf_v, iou=iou_v) 
            boxes = res[0].boxes
            
            draw_img = img.copy()
            draw = ImageDraw.Draw(draw_img)
            
            # [수정 2] 폰트 크기를 이미지 크기에 비례하게 조정 (자동 스케일링)
            font_size = max(20, int(img.size[0] / 30))
            try:
                # 윈도우 기본 폰트 경로 시도, 없으면 기본 폰트
                font = ImageFont.truetype("arial.ttf", font_size)
            except:
                font = ImageFont.load_default()

            for i, box in enumerate(boxes):
                xyxy = box.xyxy[0].tolist()
                label_idx = int(box.cls[0])
                label_name = yolo_model.names[label_idx].lower()
                
                # 색상 결정 (병해: 빨강, 나머지: 초록)
                rect_color = (255, 82, 82) if 'dis' in label_name or 'mold' in label_name else (76, 175, 80)
                
                # 박스 및 번호 그리기
                draw.rectangle(xyxy, outline=rect_color, width=5)
                label_text = f"#{i+1}"
                
                # 텍스트 배경 상자
                tw, th = draw.textbbox((xyxy[0], xyxy[1]), label_text, font=font)[2:]
                draw.rectangle((xyxy[0], xyxy[1], tw, th), fill=rect_color)
                draw.text((xyxy[0], xyxy[1]), label_text, fill=(255, 255, 255), font=font)

            st.image(draw_img, caption="AI 탐지 결과 (순번 표시)", width=400)

    with col2:
        st.write("### 📊 상세 진단 리포트")
        if not f:
            st.info("💡 왼쪽에서 사진을 업로드해 주세요.")
        elif len(boxes) == 0:
            st.warning("🔍 탐지된 객체가 없습니다. 배지가 잘 보이도록 찍어주세요.")
        else:
            st.markdown(f'<div class="result-card"><b>총 탐지:</b> {len(boxes)}개</div>', unsafe_allow_html=True)
            status_for_ai = []
            
            for i, box in enumerate(boxes):
                label_idx = int(box.cls[0])
                label_raw = yolo_model.names[label_idx].lower()
                score = float(box.conf[0]) * 100
                
                # [수정 3] 배양 상태인지 생육 상태인지 구분하여 리포트 작성
                if 'dis' in label_raw or 'mold' in label_raw:
                    d_name = disease_lookup.get(label_raw[:3], "병해 발생")
                    k_name, s_color = f"위험({d_name})", "#FF5252"
                    detail = "⚠️ 즉시 격리 및 소독이 필요합니다."
                    status_for_ai.append(d_name)
                else:
                    k_name, s_color = "정상 상태", "#4CAF50"
                    detail = "✅ 균사 배양 또는 생육이 양호합니다."
                    status_for_ai.append("정상")

                st.markdown(f'''
                    <div class="result-card" style="border-left: 5px solid {s_color};">
                        <b>#{i+1} {k_name}</b> (확신도: {score:.1f}%)<br>
                        <span style="color:{s_color};">{detail}</span>
                    </div>
                ''', unsafe_allow_html=True)

            # 판독 결과를 선택된 농장 이력에 기록 (같은 사진은 1회만)
            diseases_found = sorted({s for s in status_for_ai if s != "정상"})
            diag_key = f"{current_farm}_{f.name}_{len(boxes)}"
            if current_farm and st.session_state.get("last_diag_key") != diag_key:
                st.session_state.last_diag_key = diag_key
                farm_mgr.storage.add_diagnosis(current_farm, ", ".join(diseases_found) or "정상",
                                               {"detections": len(boxes), "source": "photo"})
            if diseases_found and current_farm:
                farm = farm_mgr.farm(current_farm)
                st.error(f"🚨 {farm['name']}에서 병해({', '.join(diseases_found)})가 탐지되었습니다. {env_context}")
                if farm.get("stage") != "disease" and st.button("🛡️ 이 농장을 '병해 긴급관리' 모드로 전환 (습도↓·온도↓·환기)"):
                    farm_mgr.update_farm(current_farm, stage="disease", auto_control=True)
                    st.success("전환 완료: 자동제어가 병해 긴급관리 기준(15~18℃, 습도 70~80%)으로 동작합니다.")

# 2. AI 처방전 섹션
            if llm_model and len(status_for_ai) > 0:
                st.markdown("---")
                loading_placeholder = st.empty()
                loading_placeholder.markdown('<div class="loading-text">⏳ 버섯 전문가가 진단서를 작성하고 있습니다...</div>', unsafe_allow_html=True)

                diseases_only = [s for s in status_for_ai if s != "정상"]
                target_status = diseases_only[0] if diseases_only else "정상 생육"
                
                current_context = search_rag(target_status)
                # 매뉴얼 제목이나 '데이터'라는 단어가 들어가면 AI가 헷갈리므로 삭제
                clean_context = current_context.replace("표고버섯 재배 통합 매뉴얼", "").replace("최종 TXT용 데이터", "").replace("~", "에서 ")

                # [수정] 프롬프트를 "명령"이 아닌 "작성 중인 문서"처럼 구성합니다.
                prompt = f"""당신은 30년 경력의 엄격하고 전문적인 버섯 재배 전문가입니다. 
                [출력 규칙]
                - "솜사탕", "음식", "섭취", "입" 같은 단어는 절대 사용하지 마십시오.
                - 균사의 모양을 설명할 때는 "솜털 모양의 균사" 또는 "거친 균사"라고 표현하십시오.
                - "먹지 마라"는 표현 대신 "제거하십시오" 또는 "폐기하십시오"라고 하십시오.

                [진단 대상]: {target_status}
                [재배사 현재 환경]: {env_context}
                [참고 자료]: {clean_context[:400]}

                보고서 내용:
                농민 여러분, {target_status} 확산을 막기 위해 다음 지침을 따르십시오.
                1. """

                try:
                    output = llm_model(
                        prompt, 
                        max_tokens=1000, 
                        temperature=0.0, # 낮을수록 헛소리가 줄어듭니다.
                        repeat_penalty=1.1, # 자기 말을 베끼는 것을 강하게 방지
                        top_p=0.5,          # 답변의 다양성을 조금 부여하여 끊김 방지
                        stop=[], # AI가 헷갈려할 키워드들에서 멈춤
                        echo=False
                    )
                    
                    full_text = output.strip() if isinstance(output, str) else output['choices'][0]['text'].strip()

                    # 후처리 필터링
                    import re
                    # --- [후처리 필터링 및 용어 치환] ---
                    raw_lines = full_text.split('\n')
                    clean_advice = []

                    for line in raw_lines:
                        line = line.strip()
                        if len(line) < 10: continue
                        
                        # [핵심] 식물 용어를 버섯 용어로 자동 강제 치환
                        line = line.replace("잎과 줄기", "갓과 대").replace("잎에", "갓에").replace("줄기에", "대에").replace("식물", "버섯")
                        line = line.replace("솜사탕을 섭취하지", "균사를 만지지")
                        line = line.replace("솜사탕을", "균사를")
                        line = line.replace("섭취한 사람들을", "오염된 배지를")
                        line = line.replace("환부를", "오염 부위를")
                        line = line.replace("식물", "버섯").replace("잎", "갓").replace("줄기", "대")
                        if "사람" in line or "환자" in line:
                            continue
                                                
                    # 4. [완성] 문장이 마침표로 끝나지 않았다면 강제로 마침표 추가
                        if not line.endswith(('.', '!', '?')):
                            line += " 하십시오."
                        
                        # 5. 번호 및 불필요한 서두 제거
                        line = re.sub(r'^\d+\.\s*|^-\s*', '', line)
                        
                        clean_advice.append(line)

                    # 최종 출력 구성
                    if len(clean_advice) >= 1:
                        final_output = f"🍄 **[AI 전문가 긴급 처방전: {target_status}]**\n\n"
                        # AI가 쓴 글을 최대한 살리되, 번호만 예쁘게 매깁니다.
                        for idx, advice in enumerate(clean_advice[:4]):
                            final_output += f"{idx+1}. {advice}\n\n"
                        ans = final_output
                    else:
                        raise ValueError("유효한 답변 부족")

                except Exception as e:
                    # AI가 엉뚱한 소리를 할 때 보여줄 안전한 기본 처방전
                    ans = f"⚠️ **{target_status} 긴급 대응 지침**\n\n"
                    ans += "1. 재배사 내 습도를 80% 이하로 낮추고 환풍기를 가동하십시오.\n\n"
                    ans += "2. 푸른곰팡이가 발생한 부위는 포자가 날리지 않게 조심스럽게 제거하십시오.\n\n"
                    ans += "3. 도구와 장화는 반드시 소독 후 사용해 추가 확산을 방지하십시오."

                loading_placeholder.empty()
                st.info(ans)

                # 피드백 버튼
                c1, c2, c3 = st.columns(3)
                if c1.button("👍 좋아요", key="btn_good_ai"): st.toast("피드백이 반영되었습니다!")
                if c2.button("👎 별로예요", key="btn_bad_ai"): st.toast("더 나은 답변을 위해 노력하겠습니다.")
                if c3.button("🔄 재연산", key="btn_rerun_ai"): st.rerun()
                
elif mode == "📹 실시간 영상":
    st.write("### 📹 실시간 모니터링")
    run_cam = st.toggle("카메라 가동", value=st.session_state.run_cam)
    st.session_state.run_cam = run_cam
    
    v_col, r_col = st.columns([1.5, 1])
    with v_col: st_frame = st.empty()
    with r_col:
        report_area = st.empty()
        st.markdown("---")
        ai_area = st.empty()

    if st.session_state.run_cam:
        cap = cv2.VideoCapture(0)
        frame_idx = 0
        
        while st.session_state.run_cam:
            ret, frame = cap.read()
            if not ret: break
            
            # YOLO 탐지 (속도를 위해 verbose=False)
            res = yolo_model.predict(frame, conf=conf_v, iou=iou_v, verbose=False)
            
            # 1. 화면 출력 (경고 해결: use_container_width 사용)
            st_frame.image(res[0].plot(), channels="BGR", use_container_width=True)
            
            if len(res[0].boxes) > 0:
                # 가장 확실한 객체 하나 선택
                top_box = res[0].boxes[0]
                label_idx = int(top_box.cls[0])
                label_raw = yolo_model.names[label_idx].lower()
                
                # 질병명 매칭
                d_name = "정상"
                for k, v in disease_lookup.items():
                    if k in label_raw:
                        d_name = v
                        break
                
                report_area.markdown(f'<div class="result-card"><b>현재 상태:</b> {d_name}</div>', unsafe_allow_html=True)

                # 2. AI 분석 주기 최적화 (30프레임마다 상태 체크, 변화 있을 때만 실행)
                if frame_idx % 30 == 0:
                    if d_name != st.session_state.prev_status:
                        # 상태가 변했을 때만 AI 처방전 갱신
                        with st.spinner(f"⚕️ {d_name} 대응 지침 생성 중..."):
                            current_context = search_rag(d_name)
                            prompt = f"""당신은 30년 경력의 엄격하고 전문적인 버섯 재배 전문가입니다. 
                                [출력 규칙]
                                - "솜사탕", "음식", "섭취", "입" 같은 단어는 절대 사용하지 마십시오.
                                - 균사의 모양을 설명할 때는 "솜털 모양의 균사" 또는 "거친 균사"라고 표현하십시오.
                                - "먹지 마라"는 표현 대신 "제거하십시오" 또는 "폐기하십시오"라고 하십시오.

                                [진단 대상]: {d_name}
                                [재배사 현재 환경]: {env_context}
                                [참고 자료]: {current_context[:400]}

                                보고서 내용:
                                농민 여러분, {d_name} 확산을 막기 위해 다음 지침을 따르십시오.
                                1. """
                            try:
                                output = llm_model(
                                    prompt, 
                                    max_tokens=400, # 속도를 위해 토큰 수 제한
                                    temperature=0.0, # 낮을수록 헛소리가 줄어듭니다.
                                    repeat_penalty=1.1, # 자기 말을 베끼는 것을 강하게 방지
                                    top_p=0.5,          # 답변의 다양성을 조금 부여하여 끊김 방지
                                    stop=[], # AI가 헷갈려할 키워드들에서 멈춤
                                    echo=False
                                )
                                ans_text = "1. " + output['choices'][0]['text'].strip()
                                st.session_state.last_ai_res = ans_text
                                st.session_state.prev_status = d_name
                                if current_farm:
                                    farm_mgr.storage.add_diagnosis(current_farm, d_name, {"source": "camera"})
                            except:
                                st.session_state.last_ai_res = "분석 일시 지연 (재시도 중)"

                # AI 결과 표시 (루프 내내 유지)
                ai_area.markdown(f'''
                    <div class="ai-box">
                        <b>⚕️ 실시간 전문가 처방 ({d_name})</b><br>
                        {st.session_state.last_ai_res}
                    </div>
                ''', unsafe_allow_html=True)

            frame_idx += 1
            if cv2.waitKey(1) & 0xFF == ord('q'): break
            
        cap.release()
        
# 사이드바 맨 아래에 종료 버튼 배치
if st.sidebar.button("🛑 Exit Program"):
    placeholder = st.empty() # 메인 화면 영역 확보
    with placeholder.container():
        st.error("### 🏁 시스템이 완전히 종료되었습니다.")
        st.write("보안을 위해 이 브라우저 탭을 닫아주십시오.")
    
    # 프로세스 종료 실행
    import os, signal
    os.kill(os.getpid(), signal.SIGTERM)