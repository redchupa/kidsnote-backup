# 🍼 Kidsnote → Notion 자동 백업

> **어린이집 알림장과 사진을, 내 노션 DB에 영원히.**
> 졸업할 때 사라지지 않게.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Runs on GitHub Actions](https://img.shields.io/badge/runs%20on-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)](#8단계-실행)
[![Notion](https://img.shields.io/badge/output-Notion-black?logo=notion)](#)

키즈노트(어린이집 알림장)에 매일 쌓이는 글과 사진을 **내 개인 노션 데이터베이스로 자동 백업**합니다. 클라우드에서 돌아가니 컴퓨터를 켜둘 필요 없고, 같은 알림장이 중복으로 들어가지 않으며, 사진의 GPS 정보는 업로드 직전 자동으로 제거됩니다.

---

## 왜 만들었나요

키즈노트는 졸업하면 접근이 끊깁니다. 우리 아이의 매일이 담긴 알림장과 사진이, 어느 날 그냥 사라집니다.

내 노션 워크스페이스에 백업해두면 — **검색되고, 정리되고, 영원히 내 것이 됩니다.**

---

## 만들어지는 결과

내 노션 DB의 한 페이지 = 알림장 하나:

```
📅 5월 12일 알림장
   날짜: 2026-05-12
   Report ID: 1390908410

   오늘은 친구들과 함께 점토놀이를 했어요. 아이가 토끼를
   만들어 자랑스럽게 보여주었답니다. 점심도 잘 먹고...

   [📷 사진 5장 — EXIF GPS 자동 제거됨]
```

매일 하나씩, 일년치 알림장이 노션 DB에 자동으로 정렬됩니다.

---

## 어떻게 작동하나요

```
   당신의 키즈노트 계정
            │
            ▼
   GitHub Actions (클라우드)        ← 컴퓨터 꺼져 있어도 OK
            │
            ▼
   GPS 제거 → 5MB 자동 압축
            │
            ▼
   당신의 노션 DB                   ← 토큰만 있는 사람만 읽기 가능
```

- **로그인**: 직접 입력한 sessionid 쿠키만 사용 (비밀번호는 코드에 등장 안 함)
- **사진**: 업로드 전 EXIF GPS·MakerNote 자동 strip + 5MB 초과시 리사이즈
- **중복 방지**: 알림장 ID 기반 dedup — 백 번 돌려도 새것만 추가
- **개인 데이터**: 당신의 노션 워크스페이스 외부로 절대 나가지 않음

---

# 시작하기 (15분)

> **준비물 체크리스트**
> - [ ] 키즈노트 계정 (PC 브라우저로 로그인 가능해야 함)
> - [ ] 노션 계정 (무료 플랜 OK) — 없으면 https://www.notion.so 가입
> - [ ] GitHub 계정 — 없으면 https://github.com 가입
> - [ ] Firefox 브라우저 — 없으면 https://www.mozilla.org/firefox 설치 (쿠키 추출에 필요)

## 1단계. 이 repo를 내 GitHub로 fork

1. 이 페이지 우측 상단 **`Fork`** 버튼 클릭
2. 다음 화면에서 **`Create fork`** 클릭
3. 그대로 페이지를 두고 다음 단계 진행

✅ 성공 신호: 자신의 GitHub 페이지(`github.com/내아이디/kidsnote-backup`)로 이동되어 있음.

## 2단계. 노션에서 백업용 데이터베이스 만들기

1. 노션 좌측 사이드바에서 **`+ 페이지 추가`** 클릭
2. 새 페이지에 **제목을 입력** (예: "키즈노트 백업")
3. 본문에 슬래시 입력: **`/database`**
4. 드롭다운에서 **`데이터베이스 - 인라인`** 선택
5. DB 위 이름란에 **"키즈노트 백업"** 등 자유롭게 입력

이제 속성(컬럼)을 정리해야 합니다:

6. **"태그"** 열의 머리글(`태그`) 클릭 → **`속성 삭제`**
7. 표 우측 끝 **`+`** 클릭 → 새 속성 추가:
   - 이름: **`날짜`**
   - 종류: **`날짜`**
8. 표 우측 끝 **`+`** 클릭 → 새 속성 추가:
   - 이름: **`Report ID`** (정확히 영문, 대소문자 일치)
   - 종류: **`숫자`**

✅ 성공 신호: 표 머리글에 **`이름`** | **`날짜`** | **`Report ID`** 세 칸이 보임.

> 💡 "이름"이 다른 언어로 보이면(`Name` 등) 그대로 둬도 됩니다 — 코드가 자동으로 인식합니다.

## 3단계. 노션 통합(Integration) 만들기 + 토큰 받기

1. 새 탭에서 https://www.notion.so/profile/integrations 접속
2. **`+ 새 통합`** 버튼 클릭
3. 폼 작성:
   - 이름: `kidsnote-backup` (자유)
   - 연결된 워크스페이스: 본인 워크스페이스 선택
   - 유형: `내부` 그대로
4. **`저장`** 클릭
5. 다음 페이지에서 **`내부 통합 시크릿`** 항목의 **`표시`** 클릭 → **`복사`** 버튼

⚠️ 이 토큰을 **메모장에 잠깐 붙여두세요** (5분 안에 GitHub에 등록할 예정). 절대 다른 사람과 공유하지 마세요.

토큰 형태 예시: `secret_AbCdEf1234...` 또는 `ntn_AbCdEf1234...` (총 50자 내외)

✅ 성공 신호: 50자 정도의 토큰 문자열이 클립보드에 있음.

## 4단계. 노션 통합을 DB에 연결

토큰을 만들었다고 끝이 아닙니다. 이 통합이 우리 DB에 쓸 수 있게 **명시적으로 연결**해줘야 합니다.

1. 2단계에서 만든 DB 페이지로 돌아가기
2. 페이지 **우측 상단의 `⋯`** (점 세 개) 클릭
3. 메뉴 하단 **`연결`** 클릭 → **`연결 검색`** 입력란 클릭
4. **`kidsnote-backup`** 검색 → 검색 결과 클릭
5. 확인 팝업에서 **`확인`** (또는 `kidsnote-backup이 새 페이지에 액세스할 수 있도록 허용`)

✅ 성공 신호: 다시 `⋯` → `연결`에 들어가면 `kidsnote-backup`이 나열됨.

## 5단계. 노션 DB ID 복사

DB 페이지를 띄운 상태에서 **브라우저 주소창 URL**을 봅니다:

```
https://www.notion.so/내워크스페이스/238f5e29c0894adfb6c4d8e1a5b2c3d4?v=abcdef...
                                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                    이 32자 hex 문자열이 DB ID
```

규칙:
- 마지막 슬래시(`/`) 다음부터 **물음표(`?`) 또는 하이픈(`-`) 이전**까지
- 보통 32자, 영문 소문자+숫자 조합
- workspace 이름이 들어간 URL이면 그 뒤에 있는 hex 부분이 ID

⚠️ DB ID를 **메모장에 붙여두세요** (다음에 GitHub에 등록).

✅ 성공 신호: 32자 hex 문자열이 클립보드에 있음.

## 6단계. 키즈노트 sessionid 쿠키 가져오기

**반드시 Firefox**로 진행하세요 (Chrome은 일부 버전에서 쿠키가 안 보입니다).

1. Firefox로 https://www.kidsnote.com 접속 후 평소처럼 로그인
2. 로그인된 화면에서 **`F12`** 키 (개발자 도구 열림)
3. 개발자 도구 상단 탭에서 **`저장소`** 클릭
   - 안 보이면 탭 줄 끝의 **`>>`** 클릭 → 목록에서 `저장소` 선택
4. 좌측 트리에서 **`쿠키`** 펼치기 → **`https://www.kidsnote.com`** 클릭
5. 우측 표가 나타남. **`이름` 열에서 `sessionid`** 행 찾기
6. 그 행의 **`값` 칸을 더블클릭** → 전체 선택된 상태에서 `Ctrl+C` 복사

쿠키 값 예시: `ycen2ydnwm2vsoj3zxe618k5nugt7j66` (32자 정도)

⚠️ 메모장에 붙여두세요.

> 💡 이 쿠키는 약 **30일 후 만료**됩니다. 만료되면 워크플로 실행 시 에러가 나는데, 그때 이 6단계만 다시 하면 됩니다.

✅ 성공 신호: 32자 정도의 쿠키 값이 클립보드에 있음.

## 7단계. GitHub Secrets에 3개 값 등록

이제 메모장에 모아둔 세 값을 GitHub fork에 안전하게 저장합니다.

1. 자신의 fork repo (`github.com/내아이디/kidsnote-backup`)로 이동
2. 상단 메뉴에서 **`Settings`** 클릭 (톱니바퀴 아이콘)
3. 좌측 사이드바 **`Secrets and variables`** 클릭 → **`Actions`** 클릭
4. 우측 상단 **`New repository secret`** (녹색 버튼) 클릭
5. 다음과 같이 등록:

| Name (정확히 일치) | Value |
|---|---|
| `NOTION_TOKEN` | 3단계에서 복사한 토큰 |
| `NOTION_DATABASE_ID` | 5단계에서 복사한 DB ID |
| `KIDSNOTE_SESSION_COOKIE` | 6단계에서 복사한 쿠키 값 |

매번 `New repository secret` → 이름 입력 → 값 붙여넣기 → `Add secret` 클릭.

> ⚠️ 값에 앞뒤 공백이 끼지 않게 주의. 메모장에서 복사할 때 줄바꿈이 같이 잡혀도 GitHub가 자동으로 trim하지만 가급적 깔끔하게.

✅ 성공 신호: 시크릿 목록에 위 3개 이름이 나열됨 (값은 *** 으로 가려짐). 등록한 시간만 표시.

## 8단계. 실행!

1. fork repo 페이지 상단 **`Actions`** 탭 클릭
2. (처음이면 "Workflows aren't running" 안내 나옴 → **`I understand my workflows, go ahead and enable them`** 클릭)
3. 좌측 목록에서 **`Kidsnote → Notion mirror`** 클릭
4. 우측 상단 **`Run workflow`** 드롭다운 클릭
5. **`limit`** 입력란은 비워둔 채 (또는 처음이라면 `3`을 넣어 안전 확인 후 다시 비워서 실행)
6. 녹색 **`Run workflow`** 버튼 클릭

5~25분 기다리면 됩니다 (알림장 개수 + 사진 분량에 따라 변동).

✅ 성공 신호:
- Actions 페이지에서 워크플로 옆 동그라미가 **녹색 체크 ✅**
- 노션 DB 페이지로 돌아가면 알림장들이 날짜순으로 들어가 있음
- 클릭하면 본문 + 사진까지 정상 표시

---

# 다음에 또 백업할 때

키즈노트에 새 알림장이 쌓이면 위 **8단계만** 다시 실행하면 됩니다. 이미 노션에 들어간 알림장은 **자동으로 skip**되고 새것만 추가됩니다.

쿠키가 만료(약 30일)되면 워크플로가 실패 — 그때 **6단계**(sessionid 추출) 다시 해서 **7단계**에서 `KIDSNOTE_SESSION_COOKIE` 시크릿을 **Update** 하면 됩니다.

---

## 자주 묻는 질문

**Q. 비용은?**
GitHub Actions 무료 한도(public repo 무제한, private repo 월 2,000분) + 노션 무료 플랜만으로 충분합니다. 1년치 백업이 한 번에 20분 정도.

**Q. 사진이 큰데 괜찮나요?**
노션 무료 플랜은 파일당 5 MiB 한도. 코드가 자동으로 EXIF 제거 → 리사이즈(1920px) → JPEG 품질 단계적 축소로 압축합니다. 키즈노트 일반 사진(3~8MB iPhone JPEG)은 100% 통과합니다.

**Q. 카카오 SSO로 로그인하는 계정도 되나요?**
sessionid 쿠키 기반이라 카카오 SSO든 일반 계정이든 차이 없습니다. 브라우저에서 정상 로그인만 되면 OK.

**Q. 매주 자동 실행은 안 되나요?**
키즈노트 로그인이 SPA로 바뀐 후 헤드리스 자동 로그인이 막혔습니다. sessionid를 수동 갱신해야 하므로 **수동 실행만 지원**합니다 (한 달에 한 번 정도 실행).

**Q. 자녀가 둘 이상이면?**
첫 번째 등록된 자녀가 기본. 다른 자녀를 백업하려면 워크플로 yml의 `ARGS=( ... )` 줄에 `--child-id <숫자>`를 추가하면 됩니다. (자녀 ID는 처음 실행 시 로그에 출력됨.)

**Q. 정말 안전한가요?**
- 모든 자격증명은 GitHub Secrets로만 저장 (코드에 등장 X)
- 사진은 EXIF GPS·MakerNote가 노션 업로드 전 메모리에서 제거됨
- 당신의 노션 워크스페이스 외부로 데이터 송신 없음
- 코드는 fork 후 직접 검토 가능 (Python 약 500줄)

**Q. 워크플로가 실패했어요**
Actions 탭에서 실패한 run 클릭 → 빨간 X 옆 스텝 클릭 → 로그 확인.
- `401` 또는 `403`: sessionid 만료 → 6단계 다시
- `Notion DB query failed`: 4단계 통합 연결 안 됐거나 5단계 DB ID 잘못됨
- `KIDSNOTE_SESSION_COOKIE missing`: 7단계 시크릿 이름 오타

---

## 기술 스택

- Python 3.12 + `requests` + `Pillow` + `piexif`
- GitHub Actions (ubuntu-latest)
- Notion API v2022-06-28 (file_uploads + databases)
- 키즈노트 비공식 API `/api/v1_2/children/<id>/reports/`

상세 코드는 [`tools/kidsnote_fetch/`](tools/kidsnote_fetch/).

---

## ☕ 후원

이 도구가 도움이 되셨다면 커피 한 잔으로 응원해 주세요. 🙏

<table>
  <tr>
    <td align="center">
      <b>토스</b><br/>
      <img src="https://raw.githubusercontent.com/redchupa/kr_baby_kit/main/images/toss-donation.png" alt="Toss 후원 QR" width="200"/>
    </td>
    <td align="center">
      <b>PayPal</b><br/>
      <img src="https://raw.githubusercontent.com/redchupa/kr_baby_kit/main/images/paypal-donation.png" alt="PayPal 후원 QR" width="200"/>
    </td>
  </tr>
</table>

- 토스/카카오뱅크: **1000-1261-7813** (우*만) · *커피 한잔은 사랑입니다*

후원과 기능은 별개입니다 — 모든 코드는 MIT로 동일하게 열려 있습니다.

---

## 라이선스

[MIT](LICENSE). 자유롭게 fork, 수정, 재배포 가능. 단 **당신의 fork는 private으로 두세요** — 키즈노트 백업은 본질적으로 자녀 개인정보입니다.
