# mypage
web
바꾼 부분

web_main.py
지금 PC에 있는 web_main.py는 로그인·마이페이지가 연결되기 전 버전이라 교체가 필요해요.

지난번에 말씀드린 python web_main.py 실행이 동작하도록 실행 코드를 추가했어요.

지금 있는 synth_proxy.py가 구버전이라 infer_available이 없어도 서버가 죽지 않게 했어요.

어느 폴더에서 실행해도 static을 찾도록 경로를 고쳤어요.

web\run_web.bat (새 파일): DB 주소 설정, venv_web 활성화, DB 테이블 생성, 서버 실행을 한 번에 해요.

start_all.bat (새 파일): 합성 서버와 웹서버를 창 두 개로 동시에 켜요.


파일 위치
D:\hairMatch_demo2\
├─ setup_env.bat            (기존 그대로)
├─ start_all.bat            ★ 새로 추가
├─ inference\               (기존 그대로)
├─ HairFastGAN\             (기존 그대로)
└─ web\
   ├─ web_main.py           ★ 교체
   ├─ db_setup_v2.py        ★ 교체 (회원 테이블 5개 추가된 버전)
   ├─ gemini_api.py         ★ 교체 (로그인 연동 버전)
   ├─ map_test_server.py    ★ 교체
   ├─ auth_api.py           ★ 새로 추가 — 회원가입/로그인 API
   ├─ mypage_api.py         ★ 새로 추가 — 마이페이지 API
   ├─ test_accounts_db.py   ★ 새로 추가 — 점검용
   ├─ run_web.bat           ★ 새로 추가
   ├─ synth_proxy.py, salons_api.py, prompts.py,
   │  face_shape_cnn.py, 09_predict.py, *.keras, *.joblib, venv_web\   (기존 그대로, 건드리지 않음)
   ├─ gen_store\            (자동 생성 — 저장한 사진이 들어감)
   └─ static\
      ├─ log.html           ★ 로그인 화면
      ├─ signin.html        ★ 회원가입 화면
      ├─ mypage.html        ★ 마이페이지
      ├─ auth.js            ★ 모든 화면 공통 로그인 처리
      ├─ index.html, a.html ★ 교체 (상단 로그인 버튼 연결된 버전)
      └─ config.js, img\    (기존 그대로 — 압축에 없으니 지우면 안 돼요)


적용 순서
백업하기: D:\hairMatch_demo2\web 폴더를 통째로 복사해 두세요 (예: web_backup).

압축 풀기: zip을 D:\에 풀고 덮어쓰기를 선택하세요. 압축에 없는 기존 파일은 그대로 남아요.

패키지 설치 (새 cmd 창):

   cd /d D:\hairMatch_demo2\web
   
   call venv_web\Scripts\activate.bat
   
   pip install sqlalchemy psycopg2-binary pillow python-multipart httpx google-genai
   
점검: PostgreSQL 없이 임시 DB로 돌려요. 마지막에 ALL PASS가 나오면 정상이에요.

   python test_accounts_db.py
   
DB 비밀번호 넣기: run_web.bat을 메모장으로 열고, 이 줄의 PASSWORD를 PostgreSQL 비밀번호로 바꾸세요.

   set "DATABASE_URL=postgresql+psycopg2://postgres:PASSWORD@localhost:5432/hairmatch_db"
   
PostgreSQL이 없다면 이 줄 앞에 ::를 붙이고, 그 아래 SQLite 줄의 ::를 지우면 돼요.

Gemini 키나 카카오 키도 같은 파일에 넣을 수 있어요.

실행: D:\hairMatch_demo2\start_all.bat을 더블클릭하면 창 두 개가 떠요. 둘 다 로딩이 끝나면 http://127.0.0.1:8000으로 접속하세요.

로그인: /log.html

회원가입: /signin.html

마이페이지: /mypage.html
