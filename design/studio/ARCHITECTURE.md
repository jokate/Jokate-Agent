# 카태 스튜디오 기준 설계 (ARCHITECTURE)

`design/studio/CAMPAIGN.md` §1–2 를 구현할 수 있는 계약으로 풀어 쓴 문서다. 스튜디오 작업(P · S · M · H · N · A · V)은 이 문서의 절 번호를 인용한다.

- 작성 상태: 1부(§1–§4, §11.1–11.2), 2부(§5–§8, §11.3), 3부(§9–§10, §11.4, §12–§13) 모두 작성됨.
- 코드 행 번호는 2026-10-10 의 `main` 기준이다. studio 브랜치의 코드는 스튜디오 작업이 연결 지점을 더한 만큼 뒤에 있다 — 번호가 어긋나면 함께 적힌 심볼 이름으로 찾는다. `pipeline.py` 는 2026-10-12 에 대조했다: `RunState`(187)는 그대로이고 `create` 는 501 → 517, `_run_stage` 는 1421 → 1524 로 뒤쪽은 100줄 넘게 뒤다. 다른 파일은 대조하지 않았다.
- owner 결정 반영 (2026-10-10): `/studio/*` 는 루프백 요청에도 토큰을 요구한다(§6.2). 작업의 종결 상태 `cancelled` 를 더했다(§4). owner 에게 물었던 12건을 확정했다(§12.6).

## 절 번호

| 절 | 제목 | 상태 |
|---|---|---|
| §1 | 개요 · 용어 | 작성됨 |
| §2 | 디렉터리 배치 | 작성됨 |
| §3 | 테이블 정의 | 작성됨 |
| §4 | 작업 상태 전이 | 작성됨 |
| §5 | 결재 | 작성됨 |
| §6 | REST | 작성됨 |
| §7 | WS | 작성됨 |
| §8 | studio MCP | 작성됨 |
| §9 | employee.yaml | 작성됨 |
| §10 | 권한 · 가드 | 작성됨 |
| §11 | 기존 코드 접점 | 작성됨 |
| §12 | 캠페인 문서와 어긋나는 점 | 작성됨 |
| §13 | 작업별 인용 절 색인 | 작성됨 |

절 번호와 소절 번호(3.1, 4.2 …)는 뒤 작업이 바꾸지 않는다. 내용이 늘면 소절을 뒤에 덧붙인다.

---

## 1. 개요 · 용어

### 1.1 범위와 표기 규칙

이 문서는 릴레이 서버 위에 얹는 studio 레이어(`relay_agent/studio/`)의 데이터 · 상태 · 외부 계약을 정한다. 앱 화면과 하네스 내용은 다루지 않는다.

| 표기 | 뜻 |
|---|---|
| 확인 필요 | 코드를 읽어 확인하지 못했다. 그 절을 구현하는 작업이 먼저 확인한다 |
| 확인 필요 (사람 — `VERIFICATION.md` 의 …) | 그 절을 구현한 작업은 끝났지만 실제 Claude Code 나 실기기로만 볼 수 있어 남은 것이다. 괄호 안에 그 확인이 적힌 `VERIFICATION.md` 줄의 작업 번호와 줄 머리를 적는다. 사람이 확인하면 그 줄 끝에 결과를 적고 여기 표기를 고친다 |
| 잠정 | 이름이나 모양을 여기서 임시로 정했다. 괄호 안의 절 또는 작업이 확정한다 |
| `[§12]` | CAMPAIGN.md 에 없거나 다르게 정한 것. §12 가 이 태그를 모아 정리한다 |
| 확정 | 뒤 작업이 바꾸려면 이 문서를 먼저 고치고 owner 승인을 받는다. 표시가 없는 것은 확정이다 |
| owner 확정 (§12.6 의 N) | owner 에게 물어 정했다. §12.6 의 그 번호가 물은 것과 정한 것을 적는다. 확정과 같게 다룬다 |

### 1.2 설계 전제 5가지 (CAMPAIGN.md 57–63행)

1. 직원 = 페르소나 파일 + 전용 대화 + 모델 설정. 직원×방마다 Claude Code 대화 하나를 `--resume` 으로 잇는다 → §2.2, §3.7, §9.
2. 직원은 studio MCP 로만 회사에 행동한다. 상태 전이와 알림은 서버가 결정론적으로 처리한다 → §4, §8.
3. 기다림 = 런 종료. 결재가 필요하면 런을 끝내고, 결정이 나면 같은 대화를 잇는다 → §4.3 의 T4 · T5.
4. 권한은 공통 전권, 담당은 지침. 치명 동작만 훅이 막는다 → §10.
5. 결재는 강제 결재(서버 · 훅이 멈춘다)와 판단 결재(직원이 스스로 올린다) 두 종류다 → §5.

### 1.3 주체

전이와 기록에 나오는 "누가"는 셋 중 하나다. DB 에는 `*_kind` 와 `*_id` 두 컬럼으로 적는다.

| 주체 | 뜻 | 식별 |
|---|---|---|
| `owner` | 나. 대시보드나 앱에서 온 인증된 요청 | `*_id` 는 NULL |
| `employee` | 직원. 런에 주입된 직원 ID 로 studio MCP 를 호출한다 | `*_id` = `employees.id` |
| `system` | 서버 자신. 큐, 엔진이 알린 런 종료, 가드 훅, 재시작 복구 | `*_id` 는 NULL |

### 1.4 용어

| 용어 | 뜻 | 테이블 |
|---|---|---|
| 컴퓨터 | 릴레이 서버 한 대. DB 하나가 컴퓨터 하나다 | 없음 |
| 프로젝트 | UE 프로젝트 폴더 + 엔진 버전 + MCP 포트 + 릴레이 repo 이름 + 정책 파일 | `projects` |
| 파트 | 직원의 직무 묶음. 지침 템플릿과 기본값을 가진다 | `departments` |
| 직원 | 페르소나 폴더 하나와 DB 행 하나 | `employees` |
| 방 | 글이 쌓이는 곳. 1:1 · 작업방 · 파트방 · 시스템 공지 · 단체방 | `rooms`, `room_members`, `messages` |
| 단체방 | owner 가 직원 여럿을 초대해 만드는 방 (`rooms.kind = 'group'`, §3.6) | `rooms`, `room_members` |
| 채용 권장 | 인사가 "이 파트에 이 프로젝트로 사람이 더 필요하다"고 owner 에게 올리는 결재 (`hire_recommendation`, §5.3). 계기는 서버가 규칙으로 찾고 인사가 판단 턴에서 올린다 | `inbox` |
| 아트 | 다섯 번째 기본 파트 (`departments.id = 'art'`). 직원이 없어도 되는 선택 파트다 (§3.4) | `departments` |
| 캠페인 | 목표를 가진 작업 묶음. 진행 방식을 가진다 | `campaigns` |
| 작업 | 사유 · 명세 · 완료 기준을 가진 일 하나 | `tasks`, `task_deps`, `task_events` |
| 결재 | owner 의 결정을 기다리는 항목 | `inbox` |
| 기기 | 페어링한 폰 | `devices` |
| 런 | 릴레이 엔진의 실행 하나 (`RunState`, `pipeline.py` 187) | studio DB 밖 |
| 턴 | 직원이 한 번 일하는 단위 = 런 하나 | studio DB 밖 |
| 작업 런 | 작업의 담당이 릴레이로 일하는 런. `tasks.run_id` 가 가리킨다 | — |
| 슬롯 | 작업 런 하나가 돌 수 있는 자리 (§4.6) | — |

### 1.5 식별자

CHECK 제약과 API · WS · MCP 가 같은 값을 쓴다.

| 묶음 | 값 |
|---|---|
| 작업 상태 (8) | `proposed` 제안 · `backlog` 대기 · `working` 진행 중 · `blocked` 결재 대기 · `review` 검수 필요 · `done` 완료 · `rejected` 기각 · `cancelled` 취소 |
| 작업 유형 (5) | `feature` · `fix` · `chore` · `doc` · `data` |
| 직원 상태 (4) | `idle` 대기 · `working` 작업 중 · `blocked` 결재 대기 · `on_leave` 휴직 |
| 방 종류 (5) | `dm` 1:1 · `task` 작업방 · `department` 파트방 · `system` 시스템 공지 · `group` 단체방 (버전 3, §3.2) |
| 진행 방식 (3) | `nonstop` 무정차 · `milestone` 고비 정차(기본) · `each` 작업마다 정차 (CAMPAIGN.md 469–476행) |
| 결재 종류 (10) | `task_proposal` 작업 제안 · `hire` 채용 · `design_approval` 설계 승인 · `structure_choice` 구조 선택 · `question` 질문 · `pre_approval` 사전 승인 · `budget` 예산 · `risky_command` 위험 명령 · `final_review` 최종 검수 · `hire_recommendation` 채용 권장 (버전 5, §3.2) |
| 결재 상태 (3) | `pending` · `decided` · `cancelled` |
| 캠페인 상태 (5) | `draft` · `running` · `paused` · `done` · `cancelled` — S-14 확정 (`campaign.py` 63 의 7종과의 대응은 §11.1) |
| 기본 파트 id (5) | `hr` 인사 · `design_doc` 기획(문서) · `program` 프로그램 · `design_setup` 기획(세팅) · `art` 아트 (버전 4. 선택 파트, §3.4) |
| 주체 (3) | `owner` · `employee` · `system` |

- **차단 결재**는 작업을 `blocked` 로 만드는 6종이다: `design_approval` · `structure_choice` · `question` · `pre_approval` · `budget` · `risky_command`.
- studio MCP `ask_owner` 의 `kind` 는 결재 종류로 이렇게 바뀐다: `question` → `question`, `choice` → `structure_choice`, `approval` → `pre_approval`. `[§12]`
- 런 상태 6종은 엔진 것을 그대로 쓴다: `pending` · `running` · `awaiting_approval` · `done` · `failed` · `cancelled` (`pipeline.py` 192). `budget_exceeded` 는 런 상태가 아니라 이벤트 종류이고, 그때 런 상태는 `awaiting_approval` 이다 (`pipeline.py` 1584–1586).
- `cancelled` 는 네 묶음에 따로 있다 — 작업 상태 · 결재 상태 · 캠페인 상태 · 런 상태. 이 문서에서 **작업 취소**는 작업을 `cancelled` 로 보내는 전이 T10 · T11(§4.2)만 가리킨다. 런을 끊는 것은 `RelayEngine.cancel`(런 상태 `cancelled`, REST 로는 `stop` — §6.4)이고, 결재가 닫히는 것은 결재 상태 `cancelled`(`inbox.cancelled`, §5.1), 캠페인을 그만두는 것은 캠페인 상태 `cancelled`(§4.10)다. 하나가 `cancelled` 가 돼도 다른 것은 따라 바뀌지 않는다. 예외는 T11 하나다 — 그 작업의 차단 결재를 닫고 멈춘 런을 정리한다 (§4.3).
- WS 이벤트 이름 (§7.4 가 확정하고 payload 를 정했다): `message.created` · `task.created` · `task.transitioned` · `task.updated` · `inbox.created` · `inbox.decided` · `employee.status` · `run.live`. §7.4 가 더한 것: `room.updated` · `room.deleted` · `room.cleared` · `inbox.cancelled` · `employee.updated` · `campaign.updated` · `project.updated` · `job.step` · `job.log` · `job.finished`.

---

## 2. 디렉터리 배치

### 2.1 코드

새 코드는 `relay_agent/studio/` 에 넣는다. 기존 `pipeline.py` · `runners.py` 에는 연결 지점만 더한다 (CAMPAIGN.md 157행).

| 파일 | 맡는 것 | 만드는 작업 | 이름 |
|---|---|---|---|
| `relay_agent/studio/__init__.py` | 패키지 | S-1 | 확정 |
| `relay_agent/studio/schema.py` | `MIGRATIONS` 목록 (§3.2) | S-1 | 확정 |
| `relay_agent/studio/store.py` | `StudioStore` — DB 를 여는 유일한 곳 (§3.1) | S-1 | 확정 |
| `relay_agent/studio/tasks.py` | 작업 상태기계 (§4) | S-2 | 확정 |
| `relay_agent/studio/inbox.py` | 결재 생성 · 결정 · 후속 동작 (§5) | S-5 | 확정 |
| `relay_agent/studio/runs.py` | 엔진 런의 정지(승인 대기 · 예산 초과)를 결재로 올리고, 작업 없는 결재의 결정을 엔진에 잇는다 (§5.8) | S-5 | 확정 |
| `relay_agent/studio/api.py` | `/studio/*` REST (§6) | S-3 | 확정 |
| `relay_agent/studio/ws.py` | `WS /studio/ws` (§7) | S-4 | 확정 |
| `relay_agent/studio/sat.py` | 행위자 토큰 `sat1.` 의 발급과 서명 검사 (§8.2 의 검사 1) | S-6 | 확정 |
| `relay_agent/studio/tools.py` | studio MCP 도구의 서버 쪽 본체 — `POST /studio/mcp/<도구>` 가 부른다 (§8.3–§8.5) | S-6 | 확정 |
| `relay_agent/studio/files.py` | 첨부가 되는 파일 — 한도 · 이름 정리 · 복사, 직원이 넘긴 경로의 검사 (§6.6) | S-6 | 확정 |
| `relay_agent/studio/runlog.py` | 런별 기록 `runs/studio/runlog/<run_id>.json` — 런 이벤트의 스킬 호출 · MCP 호출과 가드 감사 로그의 경고를 런마다 모은다. `RunSink.idle`(`after_advance`)이 `advance()` 끝마다 다시 써 덮어쓴다 (§10.7 '런별 모음') | S-11 ① | 확정 |
| `relay_agent/studio/drafts.py` | 인사의 초안 `runs/studio/drafts/<id>.json` 과 `problems[]` (§8.4) | S-6 | 확정 |
| `relay_agent/studio/employees.py` | 직원 폴더 읽기 · 쓰기, `employee.yaml` 검사와 값 읽기, 프롬프트 조립, 상태 유도, 시드 직원 (§3.15, §4.9, §9) | S-7 | 확정 |
| `relay_agent/studio/employee_git.py` | 직원 폴더의 자체 git — 폴더 전체 커밋(`commit`. `.git` 이 없으면 먼저 만든다), 이력(`history`), 커밋 찾기(`resolve`), 그 커밋의 파일 본문(`show`). 작성자 고정, 위 폴더의 저장소와 환경 변수를 따르지 않는다. 라우트 셋과 시작 때의 첫 커밋은 `api.py`, 결재 뒤의 커밋은 `runs.py` 의 `EngineHooks.commit` 이다 (§2.2, §6.4, §9.6) | H-3 | 확정 |
| `relay_agent/studio/chat.py` | 채팅 턴 실행 — `ChatTurns`: 값 읽기 · 런 만들기 · 대화 잇기 · 새 대화로 넘기기 · 답 저장, 단체방의 턴(`group`, §4.6 · §6.6), 인사의 판단 턴(`judge`, §5.3) (§3.7, §6.6, §9.5) | S-7 | 확정 |
| `relay_agent/studio/staffing.py` | 채용 권장의 계기 넷을 규칙으로 찾는다(모델을 부르지 않는다) — `Staffing`: `no_staff` · `no_placement` · `backlog` · `request` 와 일일 보고의 훑기(`sweep`), 판단 요청의 gate(되풀이 막기)와 인사에게 보낼 요약(`brief`), 제안 결재를 읽는 `proposal_of` (§5.3) | T4 · T5 | 확정 |
| `relay_agent/studio/queue.py` | 슬롯과 대기 줄 (§4.6) | S-8 | 확정 |
| `relay_agent/studio/work.py` | 작업 런 시작 · 재개 · 종료 처리 (§4.7) | S-8, S-9 | 확정 |
| `relay_agent/studio/cutoff.py` | 기다림 = 런 종료 — T4 의 답이 나간 뒤 `Config.studio_cut_after`(기본 60초)를 기다렸다가 아직 `running` 인 작업 런을 `RelayEngine.cancel` 로 끊는다. 들어오는 길은 `Cutoff.after` 하나이고 판정은 `cut` 하나다 — 타이머, 담당이 아닌 구성원의 T4 뒤 즉시 끊기, 재시작 복구가 함께 쓴다 (§4.3 T4, §5.7, §8.3, §4.8 의 1) | S-8 (서버 3단계에 앞서 메웠다) | 확정 |
| `relay_agent/studio/machine.py` | 컴퓨터 진단 — 엔진 레지스트리 · 실행 중 에디터 · 디스크 · 한도를 읽어 `GET /studio/machine` 의 응답과 `hello` 의 `machine` 을 만든다 (§6.4, §7.1). 에디터의 MCP 포트에 명령 하나를 보내는 `mcp_send` 와 `healthy` 의 뜻인 `health_check` 도 여기 있다 (M-7) | M-1 | 확정 |
| `relay_agent/studio/editor.py` | 에디터 원격 제어 — 실행 명령 줄(`command_line`), 상태(`status`), 실행(`start`), 종료(`stop`), 뷰포트 스크린샷(`screenshot`), 켜고 응답 기다리기(`bring_up` — 스크린샷과 작업 런의 에디터 검사가 함께 쓴다). 프로세스 실행 · 종료 · MCP 전송 · 대기는 `Tools` 로 받는다. 라우트 넷은 `api.py` 에 있다 (§6.4, §6.6) | M-7 | 확정 (MCP 전송 형식은 잠정) |
| `relay_agent/studio/editor_watch.py` | 런 도중의 에디터 감시 — 에디터 검사(`work.py` 의 `WorkEffects.editor_ready`)를 통과한 작업 런을 `EVERY`(30초)마다 `health_check` 로 보고, `MISSES`(3)번 연속 응답이 없으면 `work.editor_lost` 로 런을 끊는다. 작업마다 타이머 하나이고 메모리에만 있다 (§4.3 T3, §4.5) | M-7 | 확정 |
| `relay_agent/studio/devices.py` | 기기 페어링 — 메모리의 1회용 코드 장부(`Pairings`), 기기 토큰 발급, 폰에 알려 줄 서버 주소 고르기(`reach_url`). 라우트 넷은 `api.py` 에 있다 (§6.5, §3.14) | M-2 | 확정 |
| `relay_agent/studio/importer.py` | 기존 프로젝트 가져오기 — UE 프로젝트 폴더를 읽기만 해서 `.uproject` · 엔진 · MCP 포트 · 하네스 · git 을 알아내 `detected` 를 만든다(`detect`). 등록하는 라우트 `POST /studio/projects` 는 `api.py` 의 `import_project` 다 (§6.4, §3.3) | M-6 | 확정 |
| `relay_agent/studio/creator.py` | 새 UE 프로젝트 만들기 — 이름 · 부모 폴더 · 엔진 검사(`check`), 템플릿에 이름을 넣어 폴더를 쓰는 `folder` 단계, 프로젝트 파일 생성과 에디터 타깃 빌드를 돌리는 `build` 단계. 라우트와 job 은 M-8 이 `api.py` 에 만든다 (§6.4, §7.4) | M-3 | 확정 |
| `relay_agent/studio/plugins.py` | 새 UE 프로젝트의 플러그인 — 설정의 설치 목록 읽기(`catalog`), 생성 화면의 선택지(`choices`), 요청 검사(`select`), `.uproject` 등록(`register`), 폴더를 복사하고 등록하는 `install`, MCP 포트 배정(`allocate`)과 설정 파일 쓰기(`write_ini_port` · `write_mcp_json` · `write_guards`), 이 둘을 한 번에 도는 `plugins` 단계(`step`). 라우트와 job 은 M-8 이 `api.py` 에 만든다 (§6.4, §7.4) | M-4 | 확정 (폴더명 · `.mcp.json` 서버 항목 · guards 기본본은 잠정) |
| `relay_agent/studio/jobs.py` | 새 UE 프로젝트를 만드는 생성 작업 — 생성 화면의 선택지(`options`), 시작 전 검사(`admit`), job 파일(`runs/studio/jobs/<job_id>.json`), 다섯 단계를 차례로 돌리며 `job.step` · `job.log` · `job.finished` 를 내는 `Jobs`. 라우트는 `api.py` 에 있다 (§6.4, §7.4). 실패한 단계부터의 재시도(`Jobs.retry`), 만든 것의 정리(`Jobs.cleanup`), 재시작 뒤의 복구(`Jobs.recover`)도 여기 있다 | M-8 | 확정 |
| `relay_agent/studio/harness.py` | 하네스와 새 프로젝트의 등록 — game-harness 의 배치 파일 두 개를 프로젝트 폴더에 돌리고, 폴더를 릴레이 repo · git 저장소 · `projects` 행으로 만든다 (`harness` · `register` 단계, §6.4, §7.4) | M-5 | 확정 (배치 파일 인자는 잠정) |
| `relay_agent/studio/hr/` | 기본 파트 `hr` 의 원본 — `GUIDE.md`(`guide_template`) · `PERSONA.md` · `employee.yaml`(`default_models_json`). 다루는 코드는 `employees.py` 다 (§3.4, §3.15) | H-2 | 확정 (문안은 owner 승인 전) |
| `relay_agent/studio/defaults/` | 새 UE 프로젝트에 그대로 넣는 기본 파일 — `mcp_guards.json`(`.claude/mcp_guards.json` 의 기본본. 지금은 규칙이 없는 `{}`), `gitignore` · `gitattributes`(UE 일반 규칙. `harness.py` 가 쓴다). 다루는 코드는 `plugins.py` · `harness.py` 다 | M-4 | 잠정 |
| `relay_agent/studio/seed/` | 시드 직원의 파일 — `PERSONA.md` · `GUIDE.md` · `employee.yaml`. 다루는 코드는 `employees.py` 다 | S-7 | 확정 |
| `relay_agent/studio/parts/` | 기본 네 파트(기획(문서) · 프로그램 · 기획(세팅) · 아트)의 원본 — `design_doc.md` · `program.md` · `design_setup.md` · `art.md`(GUIDE 템플릿, 결재 기준 절의 빈 틀 포함)와 `defaults.yaml`(파트별 MCP · 모델 · 노력치 기본값). 사람이 읽고 고친다. 다루는 코드는 `employees.py` 다 (§3.4, §3.15) | H-1 | 확정 (문안 · 값은 owner 승인 전) |
| `relay_agent/studio/template/` | 새 UE 프로젝트의 최소 C++ 스켈레톤 — `.uproject`, 런타임 모듈, Target 둘, `Config/Default*.ini`, `Docs/README.md`. 자리표시는 `__NAME__` · `__ENGINE__` 둘이다. 다루는 코드는 `creator.py` 다 | M-3 | 확정 |
| `relay_agent/studio/guard.py` | 가드의 서버 쪽 — `check`: `POST /studio/guard/check` 의 분기 넷과 1회 허용 쓰기 (§6.4, §10.5). `profile` · `write_ctx` · `write_settings`: studio 런의 단계에 주는 사전 승인(`studio-full`) · `guard_ctx.json` · 훅 설정 파일 (§10.1, §10.2, §11.4). 엔진의 콜백 `api.guard_stage` 가 부른다. `api.py` 를 import 하지 않는다 | S-10 | 확정 |
| `relay_agent/studio/review.py` | 검수 패키지(§5.4), 통과 · 재작업 · 되돌리기의 판정과 뒤처리 — `gate` · `passed` · `reverted` · `commit` (§4.3 T7–T9 · T11) | S-12 | 확정 |
| `relay_agent/studio/campaigns.py` | 캠페인 자동 진행 — `Autopilot.look`: `running` 캠페인에서 시작할 수 있는 작업을 줄에 세우고, 작업이 모두 끝난 캠페인을 `done` 으로 넘긴다. `paused` · `cancelled` 가 된 캠페인이 줄 세운 작업은 줄에서 빼고, "배치 필요" · "선행 거절"을 시스템 방에 알린다 (§4.10 · §5.9) | S-14 | 확정 |
| `relay_agent/studio/stats.py` | 진척 집계, 일일 보고, 성장 수치 · 오늘의 결산 | S-13 | 확정 |
| `relay_agent/studio/push.py` | FCM 발송 — 서비스 계정 키로 액세스 토큰을 받아 HTTP v1 로 보낸다(`Sender`). 스트림 이벤트의 커밋과 엔진 알림을 듣고 등급을 붙여(`classify`) 워커 스레드 하나가 기기마다 보내며, 죽은 토큰을 비운다(`Pusher`). 방해 금지(`quiet_now`) · 방 음소거 · 미결 결재 재알림(`Pusher.remind` · `watch`)도 여기서 한다. 서버에 거는 것은 `attach` 다 (§5.9, §11.3) | N-1, N-2 | 확정 |

패키지 밖에 두는 것:

| 파일 | 맡는 것 | 만드는 작업 |
|---|---|---|
| `mcp_servers/studio.py` | studio MCP 서버 (§8). 기존 `mcp_servers/` 의 `katae.py` · `handoff.py` 옆 | S-6 |
| `relay_agent/studio_guard.py` | PreToolUse 훅 (§10) — 도구 호출마다 뜨는 단독 프로세스다. 판정(`judge`)과 감사 로그(`write_audit`)를 맡고, 표준 라이브러리만 쓰며 `relay_agent` 를 import 하지 않는다. 서버 쪽 짝은 `relay_agent/studio/guard.py` 다. 위치는 S-10 확정 — 기존 `relay_agent/backup_hook.py` 와 같은 층 | S-10 |
| `relays/employee-chat.yaml` | 채팅용 단일 단계 릴레이 | S-7 |
| `prompts/employee-chat.md` | 채팅 릴레이의 단계 프롬프트 — `relays/employee-chat.yaml` 의 `prompt` 가 가리킨다 | S-7 |
| `relay_agent/campaign.py` | 기존 파일을 확장한다 (새로 만들지 않는다) — `CampaignRunner.studio` · `advance_studio` (§4.10) | S-14 |
| `relays/studio-campaign.yaml` · `studio-plan.yaml` · `studio-task.yaml` | CAMPAIGN.md 의 작업을 돌리는 캠페인 릴레이와 그것이 쓰는 내부 릴레이 둘(계획 · 작업). 전 단계 Opus 고정 | 작업 없음 — 캠페인 실행용 (CAMPAIGN.md 489행) |

### 2.2 데이터

기준 경로는 `Config.runs_dir` 이다 (`config.py` 18, 기본 `<저장소>/runs`).

```
runs/
  history.sqlite                  기존. 세션 · 턴 · 이벤트 (config.py 20). 그대로 둔다
  usage.sqlite                    기존 (config.py 19)
  campaigns/<cid>.json            기존 CampaignRunner 저장 (campaign.py 118, 124–139)
  locks/<sha1 앞 16자>.lock       기존 폴더 락 (pipeline.py 809–811)
  <run_id>/                       기존 런 폴더 (pipeline.py 325–326). run.json · notes.jsonl 등
  studio/
    studio.sqlite                 §3 의 12개 테이블
    studio.sqlite.bak-v<n>        마이그레이션 직전 복사본 (§3.2)
    employees/<employee_id>/
      PERSONA.md                  정체성
      GUIDE.md                    파트 지침 · 결재 기준
      MEMORY.md                   배운 점
      employee.yaml               파트 · 릴레이 · MCP · 모델 · 노력치 (§9)
    projects/<project_id>/
      POLICY.md                   프로젝트 정책(결재 기준)
    attachments/<글 id 또는 결재 id>/  첨부 파일 (§6.6)
    jobs/<job_id>.json            프로젝트 생성 작업 (§6.4)
    drafts/<draft_id>.json        인사의 초안 (§8.4)
    audit/guard-<YYYYMMDD>.jsonl  가드 감사 로그 (§10.7)
    runlog/<run_id>.json          런별 기록 — 스킬 · MCP · 가드 경고 (§10.7)

~/.katae/app/                     저장소 밖. 앱 갱신 (A-13, §6.4) — latest.json + APK. owner 가 손으로 두고 서버는 읽기만 한다 (Config.studio_app_dir)
```

- 직원 폴더의 자체 git 이력(H-3)은 `employees/<employee_id>/.git` 에 둔다 (H-3 확정. `studio/employee_git.py`). 직원 폴더마다 저장소가 하나이고, 서버는 그 `.git` 만 가리켜 git 을 부른다 — 위 폴더의 저장소(이 저장소)로 올라가지 않는다. 커밋 작성자는 `agent-katae` 로 고정이고 전역 git 설정을 읽지 않는다. `.git` 이 없는 기존 폴더는 서버가 시작할 때 지금 내용 그대로 첫 커밋을 받는다. 커밋하는 때와 순서는 §9.6 이다.
- 검증용 시드 직원(S-7)의 원본은 `runs/` 밖, 저장소의 `relay_agent/studio/seed/` 에 있다 (`PERSONA.md` · `GUIDE.md` · `employee.yaml`. `MEMORY.md` 는 채용 때처럼 빈 파일로 시작한다). 서버가 조건이 맞을 때 `employees/emp_5eed0001/` 로 옮겨 쓴다 (§3.15). 내용은 임시값이다. 설치본에도 들어가도록 `pyproject.toml` 의 `[tool.setuptools.package-data]` 에 `seed/*` 를 적는다.
- 새 UE 프로젝트의 스켈레톤(M-3)도 `runs/` 밖, 저장소의 `relay_agent/studio/template/` 에 있다. seed 처럼 `__init__.py` 가 없는 폴더라 `pyproject.toml` 의 package-data 에 `template/**/*` 로 적어 설치본에도 실린다. 저장소 `.gitignore` 의 `docs/` 가 윈도에서는 템플릿의 `Docs/` 에도 걸리므로 그 폴더만 다시 포함시켜 두었다.
- studio 런이면 기존 런 폴더 `runs/<run_id>/` 에 `guard_ctx.json` 과 훅 설정 파일이 더 생긴다 (§10.2, §11.4).
- `runs/` 는 `.gitignore` 에 들어 있다 (5행). 그래서 `runs/studio/` 의 DB 와 직원 폴더는 이 저장소의 git 에 올라가지 않는다.

### 2.3 파일과 DB 의 경계

| 내용 | 기준이 되는 곳 | 이유 |
|---|---|---|
| 직원의 지침 · 기억 · 모델 설정 | 직원 폴더의 파일 | 사람이 읽고 고치며, 버전 이력이 필요하다 (H-3) |
| 직원의 이름 · 파트 · 배치 · 상태 | `employees` 테이블 | 목록 조회와 FK 에 쓴다 |
| 프로젝트 정책 | `POLICY.md` | 프롬프트에 그대로 얹는다 |
| 작업 · 결재 · 방 · 글 | DB | 전이와 조회가 트랜잭션이어야 한다 |
| 런의 진행과 대화 | 기존 `runs/<run_id>/` 와 `history.sqlite` | 엔진 소유. studio 는 id 로 가리키기만 한다 |

`employee.yaml` 의 파트 값(`department`)은 `employees.department_id` 의 사본이다. 어긋나면 DB 를 따르고, 다르게 쓰려는 요청은 거부한다. 파트는 채용 뒤 바뀌지 않는다 (§9.6).

---

## 3. 테이블 정의

아래 DDL 이 기준이다. `schema.py` 의 버전 1 은 이 절과 글자 단위로 같아야 한다 (주석 제외).

### 3.1 공통 규칙

**저장소 형태.** `StudioStore` 는 `HistoryStore`(`history.py` 72–95)와 같은 형태다: `sqlite3.connect(path, check_same_thread=False)`, `row_factory = sqlite3.Row`, `threading.Lock` 하나, `_exec(sql, params)` 는 커서를, `_rows(sql, params)` 는 `list[dict]` 를 돌려준다.

다른 점은 넷이다. `[§12]`

1. 연결 직후(트랜잭션 밖에서) `PRAGMA foreign_keys=ON` 을 실행한다. `HistoryStore` 는 켜지 않는다.
2. `isolation_level=None` 으로 열고 `BEGIN IMMEDIATE` · `COMMIT` 을 직접 쓴다. 여러 문장을 한 번에 써야 하는 전이(§4.5) 때문이다.
3. 여러 문장을 묶는 `transaction()` 컨텍스트를 둔다. 잠금을 쥔 채 `BEGIN IMMEDIATE` 로 시작하고, 예외가 나면 `ROLLBACK` 한다. 안에서는 컨텍스트가 준 `Tx` 의 실행 함수(`exec` · `rows` · `row`)만 쓴다 (잠금 재진입을 피한다). CRUD 메서드에는 그 `Tx` 를 `tx=` 로 넘긴다. 트랜잭션 안에서 빠뜨리면 멈춰 서는 대신 `StudioStoreError` 를 낸다.
4. 스키마 버전을 관리한다 (§3.2). `HistoryStore` 는 버전 없이 `PRAGMA table_info` 와 `ALTER` 로 처리한다 (`history.py` 80–84).

`connect` 의 `timeout` 은 30초로 둔다 (서버 두 개가 동시에 뜰 때 §3.2 의 잠금을 기다린다). 저널 방식은 기본값을 쓴다.

**DB 를 여는 프로세스.** 서버 프로세스만 연다. S-1 은 저장소만 만든다. 서버 시작에서 `StudioStore(cfg.studio_db)` 를 여는 연결은 S-3 이 붙인다 (자리 잠정, S-3). studio MCP 서버(별도 프로세스)는 DB 를 직접 열지 않고 서버 API(`/studio/mcp/*`)를 부른다 (§8.1). 가드 훅도 DB 를 열지 않고 `POST /studio/guard/check` 를 부른다 (§6.4).

**타입 규칙.**

| 종류 | 표현 |
|---|---|
| 시각 | `TEXT`. `history.py` 의 `_now()`(68–69)와 같은 형식 — `datetime.now().isoformat(timespec="seconds")`, 예 `2026-10-10T13:10:47`. 이 컴퓨터의 지역 시각이고 시간대 표시가 없다. 앱이 여러 컴퓨터를 볼 때의 처리는 §6.1 이다 (`GET /studio/machine` 의 `now` · `utc_offset_minutes`) `[§12]` |
| 불리언 | `INTEGER` 0/1 + `CHECK`. 저장소는 `bool` 로 돌려준다 |
| 열거 | `TEXT` + `CHECK (… IN (…))`. 값은 §1.5 |
| JSON | 이름이 `_json` 으로 끝나는 `TEXT`. 저장소가 쓸 때 직렬화하고, 읽을 때 파싱해 접미사를 뗀 키로 돌려준다 (`payload_json` → `payload`). 예외는 하나다: `inbox.decision_json` 은 `decision_data` 키로 주고받는다 — 접미사를 떼면 `decision` 컬럼과 겹친다. REST 의 결정 요청에서는 `data` 필드다 (§5). DB 에는 `json_valid` 검사를 두지 않는다 |
| 엔티티 id | `TEXT`. 접두사 + `uuid4().hex[:8]`. 접두사: `prj_` · `dep_` · `emp_` · `room_` · `cmp_` · `task_` · `inb_` · `dev_`. 시드 행은 고정 id (§3.15) |
| 로그 id | `messages` · `task_events` 는 `INTEGER PRIMARY KEY AUTOINCREMENT` (순서가 곧 id) |
| 주체 | `<역할>_kind` + `<역할>_id`. `kind = 'employee'` 일 때만 id 가 있다 (CHECK) |
| 다른 저장소를 가리키는 값 | `repo`(RepoRegistry 이름) · `run_id`(RunState) · `session_id`(history.sqlite) · `conversation_id`(Claude Code 대화)는 FK 가 없다. 쓸 때 코드가 검사한다 |

**삭제 규칙.** 행을 지우는 테이블은 `task_deps` 뿐이다. 나머지는 보관 컬럼(`archived_at` · `retired_at` · `left_at` · `revoked_at`)이나 종결 상태로 표시한다. 그래서 FK 에 `ON DELETE` 절을 쓰지 않는다 (기본 동작 = 참조가 남아 있으면 거부).

**반환 규칙.** 한 행은 `dict`, 여러 행은 `list[dict]`, 없으면 `None`. CRUD 메서드 이름은 `create_<단수>` · `get_<단수>` · `list_<복수>` · `update_<단수>` 다. `tasks.status` 는 `update_task` 로 바꿀 수 없고 `tasks.py` 만 바꾼다 (§4.5).

### 3.2 스키마 버전과 마이그레이션

- 버전은 `PRAGMA user_version` 에 둔다. 0 은 빈 DB 다.
- `schema.py` 의 `MIGRATIONS` 는 `(버전, SQL 문장 목록)` 을 버전 오름차순으로 담는다. 버전 1 = §3.3–§3.14 의 테이블 12개 + 인덱스 + §3.15 의 시드. 버전 2 = `stream_events` (§7.2). 버전 3 = `rooms` 를 다시 만든다 (이 절 끝의 '버전 3'). 문장은 인자 없는 SQL 이다. 시각이 필요한 시드는 §3.15 의 SQL 식으로 채운다. 버전 4 = 아트 파트와 그 파트방 (§3.15). 버전 5 = `inbox` 를 다시 만들어 `hire_recommendation` 을 CHECK 에 넣고 `hire_signals` 를 더한다 (이 절 끝의 '버전 5').

`StudioStore` 를 만들 때의 순서:

1. 폴더를 만들고 연결한다. `PRAGMA foreign_keys=ON`.
2. `PRAGMA user_version` 을 읽는다. 코드의 최신 버전보다 크면 예외를 내고 studio 레이어를 시작하지 않는다 (옛 코드가 새 DB 를 건드리지 않게 한다).
3. 올릴 것이 있고 DB 가 비어 있지 않으면(버전 ≥ 1) 올리기 전의 파일을 `studio.sqlite.bak-v<올리기 전 버전>` 으로 복사해 둔다. 같은 이름이 있으면 덮는다. 복사하는 때는 4의 트랜잭션 안이다.
4. 현재 버전보다 큰 단계를 순서대로 실행한다. 단계 하나는 한 트랜잭션이다: `BEGIN IMMEDIATE` → `user_version` 을 다시 읽어 이미 올라가 있으면 `ROLLBACK` 하고 건너뛴다 → 이 프로세스가 처음 실행하는 단계이고 다시 읽은 버전이 1 이상이면 3의 복사를 한다 (이름의 버전은 다시 읽은 값) → 문장을 하나씩 실행 → `PRAGMA user_version = <버전>` → `COMMIT`. 예외가 나면(복사 실패 포함) `ROLLBACK` 하고 시작을 멈춘다. 복사를 잠금 안에서 하는 이유: 잠금 밖이면 늦게 뜬 서버가 다른 서버가 올린 뒤의 파일로 옛 버전 이름의 복사본을 덮는다. 쓰기 잠금을 쥐었고 아직 쓰기 전이라 파일은 다시 읽은 버전 그대로다 (기본 저널 방식 전제, §3.1).

규칙:

- `executescript` 를 쓰지 않는다 (먼저 커밋해 버려 트랜잭션이 깨진다).
- 내보낸 단계는 고치지 않는다. 바꿀 것은 새 버전으로 덧붙인다. 내림은 없다.
- 컬럼 · 테이블 삭제와 CHECK 변경(테이블을 다시 만들어야 한다)은 owner 승인을 받는다.
- 서버를 두 번 띄워도 4의 재확인 때문에 한 번만 돈다 (S-1 완료 기준).
- 실패한 단계 뒤에 `user_version` 은 그대로다. 단계의 트랜잭션과 함께 되돌아간다 (S-1 테스트로 확인).

**버전 3 — `rooms` 를 다시 만든다 (단체방).** `kind` 의 CHECK 에 `group` 을 넣고 `exchange_count` 칸을 더한다. CHECK 변경이라 위 규칙의 owner 승인 사항이고, owner 가 단체방을 요청하며 정했다 (2026-10-12). 버전 1 의 문장(§3.6)은 고치지 않고 아래 7문장을 새 버전으로 덧붙였다. `schema.py` 의 버전 3 이 이 블록과 같다 (`tests/test_studio_store.py` 가 대조한다).

```sql
PRAGMA defer_foreign_keys = ON;                          -- 이 트랜잭션의 FK 검사를 COMMIT 까지 미룬다
CREATE TABLE rooms_hold AS SELECT * FROM rooms;          -- 행을 옮겨 둔다
DROP TABLE rooms;                                        -- 인덱스 rooms_one_system 도 함께 없어진다
CREATE TABLE rooms (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('dm', 'task', 'department', 'system', 'group')),
    title           TEXT NOT NULL DEFAULT '',
    employee_id     TEXT UNIQUE REFERENCES employees(id),
    task_id         TEXT UNIQUE REFERENCES tasks(id),
    department_id   TEXT UNIQUE REFERENCES departments(id),
    owner_read_id   INTEGER NOT NULL DEFAULT 0,
    muted           INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    exchange_count  INTEGER NOT NULL DEFAULT 0,          -- 단체방에서 직원끼리 주고받은 턴의 수 (§3.6)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT,
    CHECK ((kind = 'dm') = (employee_id IS NOT NULL)),
    CHECK ((kind = 'task') = (task_id IS NOT NULL)),
    CHECK ((kind = 'department') = (department_id IS NOT NULL))
);
INSERT INTO rooms (id, kind, title, employee_id, task_id, department_id, owner_read_id, muted,
                   created_at, updated_at, archived_at)
SELECT id, kind, title, employee_id, task_id, department_id, owner_read_id, muted,
       created_at, updated_at, archived_at
FROM rooms_hold ORDER BY rowid;                          -- 행의 순서도 그대로
DROP TABLE rooms_hold;
CREATE UNIQUE INDEX rooms_one_system ON rooms(kind) WHERE kind = 'system';
```

- `messages` · `room_members` 가 `rooms` 를 참조하고 FK 가 켜져 있다. 트랜잭션 안에서는 `PRAGMA foreign_keys` 를 끌 수 없으므로(아무 일도 하지 않는다) `defer_foreign_keys` 로 검사를 COMMIT 까지 미룬다. 이 값은 COMMIT · ROLLBACK 때 스스로 꺼진다. 실행기(`StudioStore._apply_step`)는 고치지 않았다.
- 순서가 요점이다. `DROP TABLE` 은 행을 지우는 것으로 셈해 그 방을 가리키던 행만큼 위반 수를 올리고, **같은 이름**의 새 테이블에 같은 `id` 의 행을 되넣으면 그만큼 내린다. 그래서 COMMIT 때 위반이 0 이다. 다른 이름(`rooms_new`)으로 만들어 `RENAME` 하는 순서는 쓰지 않는다 — 올라간 위반 수를 내릴 길이 없다.
- 되넣지 못한 방이 하나라도 있으면 그 방을 가리키는 행이 위반으로 남아 COMMIT 이 `FOREIGN KEY constraint failed` 로 실패하고, 단계 전체(테이블 · 행 · `user_version`)가 되돌아간다 (테스트로 확인). 올리기 전부터 있던 고아 행은 이 단계가 만들지도 고치지도 않는다.
- 12개 테이블 · 인덱스 21개 · 다른 테이블의 행은 그대로다. 옛 방의 `exchange_count` 는 0 이다. 올리기 전의 파일은 `studio.sqlite.bak-v2` 로 남고 (위 3), 버전 3 의 DB 는 옛 서버가 열지 못한다 (위 2) — 되돌리려면 서버를 내리고 그 복사본을 쓴다.

**버전 5 — `inbox` 를 다시 만든다 (채용 권장).** `kind` 의 CHECK 에 `hire_recommendation` 을 넣고, 같은 단계에서 `hire_signals` 를 더한다. CHECK 변경이라 위 규칙의 owner 승인 사항이고, owner 가 채용 권장을 요청하며 정했다 (2026-10-12). §3.13 의 DDL 은 버전 1 의 문장이라 고치지 않았다. `schema.py` 의 버전 5 가 아래 블록과 같다 (`tests/test_studio_store.py` 가 대조한다).

```sql
PRAGMA defer_foreign_keys = ON;
CREATE TABLE inbox_hold AS SELECT * FROM inbox;
DROP TABLE inbox;
CREATE TABLE inbox (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN
                      ('task_proposal', 'hire', 'design_approval', 'structure_choice', 'question',
                       'pre_approval', 'budget', 'risky_command', 'final_review', 'hire_recommendation')),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'decided', 'cancelled')),
    title             TEXT NOT NULL,
    project_id        TEXT REFERENCES projects(id),
    task_id           TEXT REFERENCES tasks(id),
    requester_kind    TEXT NOT NULL CHECK (requester_kind IN ('employee', 'system')),
    requester_id      TEXT REFERENCES employees(id),
    run_id            TEXT,
    payload_json      TEXT NOT NULL DEFAULT '{}',
    decision          TEXT,
    decision_comment  TEXT NOT NULL DEFAULT '',
    decision_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    closed_at         TEXT,
    reminded_at       TEXT,
    delivery          TEXT NOT NULL DEFAULT 'none'
                      CHECK (delivery IN ('none', 'pending', 'delivered')),
    delivered_at      TEXT,
    CHECK ((requester_kind = 'employee') = (requester_id IS NOT NULL)),
    CHECK ((status = 'pending') = (closed_at IS NULL)),
    CHECK ((status = 'decided') = (decision IS NOT NULL)),
    CHECK ((delivery = 'delivered') = (delivered_at IS NOT NULL)),
    CHECK (delivery = 'none' OR status = 'decided')
);
INSERT INTO inbox (id, kind, status, title, project_id, task_id, requester_kind, requester_id, run_id,
                   payload_json, decision, decision_comment, decision_json, created_at, closed_at, reminded_at,
                   delivery, delivered_at)
SELECT id, kind, status, title, project_id, task_id, requester_kind, requester_id, run_id,
       payload_json, decision, decision_comment, decision_json, created_at, closed_at, reminded_at,
       delivery, delivered_at
FROM inbox_hold ORDER BY rowid;
DROP TABLE inbox_hold;
CREATE INDEX inbox_status ON inbox(status, created_at);
CREATE INDEX inbox_task ON inbox(task_id);
CREATE INDEX inbox_run ON inbox(run_id) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX inbox_one_pending_per_task ON inbox(task_id)
    WHERE status = 'pending' AND task_id IS NOT NULL;
CREATE INDEX inbox_undelivered ON inbox(run_id) WHERE delivery = 'pending';
CREATE TABLE hire_signals (
    key            TEXT PRIMARY KEY,
    department_id  TEXT,
    project_id     TEXT,
    trigger        TEXT NOT NULL,
    asked_at       TEXT NOT NULL
);
```

- `inbox` 를 참조하는 테이블은 없다. 그래도 버전 3 과 같은 순서를 쓴다(`defer_foreign_keys` → 옮겨 두기 → `DROP` → 같은 이름으로 만들기 → 열 18개를 이름으로 적어 되넣기 → 인덱스 5개). 행의 순서와 `id` 는 그대로고, `partial unique` 인덱스 `inbox_one_pending_per_task` 도 같다. 올리기 전의 파일은 `studio.sqlite.bak-v4` 로 남는다 (위 3).
- `hire_signals` 는 '판단 요청'을 마지막으로 낸 때를 키(파트 · 프로젝트)마다 하나 둔다 (§5.3 되풀이 막기). 열린 권장과 넘긴 권장은 `inbox` 행 자체가 기록이라 여기 쓰지 않는다 — 판단 요청은 결재 행이 생기지 않아 따로 둔다. 훅이 듣지 못한 요청의 기록은 되돌린다(§5.3).

### 3.3 projects

```sql
CREATE TABLE projects (
    id              TEXT PRIMARY KEY,                    -- prj_<hex8>
    name            TEXT NOT NULL UNIQUE,
    path            TEXT NOT NULL,                       -- UE 프로젝트 폴더(.uproject 가 있는 곳) 절대 경로
    engine_version  TEXT NOT NULL DEFAULT '',            -- 예: '5.7'
    mcp_port        INTEGER CHECK (mcp_port IS NULL OR mcp_port BETWEEN 1 AND 65535),
    repo            TEXT NOT NULL UNIQUE,                -- RepoRegistry 이름 (FK 없음)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT
);
CREATE UNIQUE INDEX projects_mcp_port ON projects(mcp_port)
    WHERE mcp_port IS NOT NULL AND archived_at IS NULL;
```

- `repo` 는 넣을 때 `RepoRegistry.get(name)`(`repos.py` 138)으로 검사한다. `RepoRegistry.match(path)`(143)가 돌려주는 저장소가 `repo` 와 같아야 한다.
- 워크스페이스 방식 · verify 명령 · MCP 목록은 `RepoSpec`(`repos.py` 30–50)을 따르고 이 테이블에 복제하지 않는다.
- 정책은 `runs/studio/projects/<id>/POLICY.md` 에 둔다 (§2.2).
- 프로젝트 하나에 repo 하나, repo 하나에 프로젝트 하나다. `[§12]`

### 3.4 departments

```sql
CREATE TABLE departments (
    id                   TEXT PRIMARY KEY,               -- 기본 5종은 고정 id, 사용자 추가는 dep_<hex8>
    name                 TEXT NOT NULL UNIQUE,
    builtin              INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
    default_relay        TEXT,                           -- relays/<이름>.yaml 의 이름. NULL = 작업 릴레이 없음
    default_mcp_json     TEXT NOT NULL DEFAULT '[]',     -- MCP 서버 이름 목록
    default_models_json  TEXT NOT NULL DEFAULT '{}',     -- employee.yaml 의 chat · work · cap 과 같은 모양 (§9.2)
    guide_template       TEXT NOT NULL DEFAULT '',       -- GUIDE.md 템플릿 본문
    sort_order           INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    archived_at          TEXT
);
```

- `builtin = 1` 인 행은 보관할 수 없고 id 를 바꿀 수 없다.
- 프로그램 성격 파트 (S-11 ②, `studio/gate.py` 의 `needs_harness`): id 가 `program` 이거나 `default_relay` 가 `auto` 인 파트다. 이 파트의 직원은 하네스(CLAUDE.md · Docs)가 없는 프로젝트에 배치 · 채용할 수 없다(`harness_missing`). 표시 열을 따로 두지 않은 것은 마이그레이션 · REST 계약 · openapi · 앱 UI 로 번지기 때문이고, 기본 릴레이가 `auto` 인 것은 `program` 하나라 기존 칸으로 구분된다. 한계: 직원의 `employee.yaml` 이 릴레이를 `auto` 로 덮어쓴 경우는 보지 않고(feature 코드 변경은 T6 가 파트와 무관하게 막는다), 이미 배치된 직원은 다시 검사하지 않는다.
- 기본 릴레이: `design_doc` = `doc-write`, `design_setup` = `editor-task`, `program` = `auto`, `art` = `editor-task`, `hr` = NULL (CAMPAIGN.md 296행. `art` 는 T3 에서 더했다. 이름의 파일이 모두 `relays/` 에 있다).
- `guide_template` · `default_mcp_json` · `default_models_json` 의 기본 5종 내용은 저장소의 원본 파일에 있고, 서버가 시작할 때 비어 있는 칸만 그 원본으로 채운다 (H-1 확정. 새 마이그레이션은 쓰지 않는다 — 이미 도는 DB 에도 같은 길로 닿는다). `hr` 의 원본은 `relay_agent/studio/hr/` (H-2. `guide_template` · `default_models_json` 두 칸), 나머지 넷의 원본은 `relay_agent/studio/parts/` 다 (§3.15). 값이 있는 칸은 덮어쓰지 않는다 — 고친 값은 다시 켜도 그대로다. `default_relay` 는 시드가 넣고 동기화하지 않는다. 채움은 칸 단위다: `default_models_json` 은 칸 전체가 비었을 때(`{}`)만 채우고 `chat` · `work` · `cap` 을 키 단위로 합치지 않는다 — owner 가 적은 값에 원본의 값을 섞으면 §9.4 로 검사받지 않은 조합이 저장된다. 일부러 비운 칸(`''` · `[]` · `{}`)도 다음 시작에 다시 채워진다 ('비움'을 기억하는 칸은 없다). 직원 한 명의 MCP 만 비우려면 그 직원의 `employee.yaml` 에 `mcp: []` 를 적는다 (§9.2).
- 아트 (T3 확정): 다섯 번째 기본 파트다. `id = art`, `name = 아트`, `builtin = 1`, `default_relay = editor-task`, 파트방 `room_dep_art`. 마이그레이션 버전 4 가 만든다 (§3.15) — 빈 DB 도 이미 도는 DB 도 같은 길이다. 선택 파트다: 직원이 없어도 자동 진행(§4.10)이 막히지 않고 '배치 필요' 알림(§5.9)도 아트 때문에 생기지 않는다 — 두 곳은 파트를 보지 않고 작업의 담당만 본다. 원본은 `parts/art.md` 와 `defaults.yaml` 의 `art`(`mcp [unreal]`, `chat` sonnet/medium, `work` edit·verify, `cap` opus/high)다.
- 새 파트 제안 (T3 확정. 경로는 §5.5 · §8.4): 인사의 채용 초안은 파트를 하나 함께 제안할 수 있다 — `draft` 의 `part_name` · `part_job`(하는 일) · `part_relay`(기본 릴레이) · `part_models`(`chat` · `work` · `cap` 의 YAML). 승인하면 한 트랜잭션에서 파트 행(`id = dep_<hex8>`, `builtin = 0`, `sort_order` = 가장 큰 값 + 1, `default_mcp` = `[]`, `guide_template` = '맡은 일: ○○' 줄과 결재 기준 빈 틀)과 파트방을 먼저 만들고 이어 직원 · 1:1 방 · 구성원을 만든다. 반려하면 아무것도 생기지 않는다. 파트의 id 는 초안을 저장할 때 서버가 정해 `department_id` 로 돌려준다.
- 사용자 파트 (H-1 확정. 경로는 §6.4): `POST /studio/departments` 가 행(`id = dep_<hex8>`, `builtin = 0`, `sort_order` = 가장 큰 값 + 1)과 파트방(`rooms.kind = 'department'`, `id = room_<hex8>`, `title` = 파트 이름, §3.6)을 한 트랜잭션에서 만들고 그 방의 `room.updated` 를 낸다. 파트 전용 이벤트는 없다 (§7.4). `PATCH` 는 준 칸만 바꾼다: `default_relay` 의 `null` · 빈 문자열은 '릴레이 없음'이고 나머지 칸의 `null` 은 빈 값이다. 이름이 바뀌면 파트방의 `title` 도 같이 바꾸고 `room.updated` 를 낸다. 바뀌는 것이 없으면 쓰지 않고 이벤트도 없다. `builtin` 파트도 `PATCH` 할 수 있다 — 바꿀 수 없는 것은 id 와 `builtin` 뿐이다.
- 파트 쓰기의 검사 (H-1 확정): `name` 은 앞뒤 공백을 떼고, 비면 `missing_fields`, 보관된 파트를 포함한 다른 파트와 겹치면 `conflict`(`field = name`)다. `default_relay` · `default_models` 는 `employee.yaml` 의 같은 이름 키로 보고 §9.4 로 검사한다 (`employees.defaults_problems`) — 걸리면 `precondition_failed` 이고 `failed[]` 가 그 코드들이다. `default_models` 의 키는 `chat` · `work` · `cap` 뿐이다(그 밖은 `unknown_key`). `default_mcp` 는 이름 목록인지와 `studio` 가 없는지만 본다(`mcp_unknown`). MCP 레지스트리에 있는지는 보지 않는다 — 없는 이름은 런에서 `mcp_missing` 이벤트로 드러난다. 대조하지 않는 까닭은 파트가 프로젝트에 묶이지 않아서다: `unreal` 처럼 프로젝트 `.mcp.json` 에 있는 서버는 레지스트리에 없는 것이 정상이고, 런의 프로젝트 맥락에 있으면 붙는다. 어디에도 없을 때 `_attached_mcp`(`pipeline.py`)는 이벤트만 남기고 런을 막지 않는다 — 그 서버 없이 일이 되는지는 릴레이에 달렸다. 프로젝트 맥락이 없는 런은 프로젝트 MCP 를 찾지 않아 같은 이벤트가 남는다. `employee.yaml` 에 직접 적은 `mcp` 는 §9.4 가 레지스트리와 대조한다. `default_mcp` 에 `unreal` 이 있으면 `mcp_port` 가 있는 프로젝트의 작업 런은 에디터 검사(M-7, §4.3 T3) 대상이 된다.
- 보관 (H-1 확정): `POST /studio/departments/{id}/archive` 는 `builtin` 이거나 재직(퇴사하지 않은) 직원이 있으면 `precondition_failed` 다. 보관하면 파트방도 같이 보관하고 `room.updated` 를 낸다. 보관한 파트방에는 owner 글은 남지만 턴은 없고, `continue` 와 직원의 `room_post` 는 거절된다. 이미 보관된 파트는 같은 행을 돌려주고 이벤트가 없다. 보관된 파트는 `GET /studio/departments` 와 인사의 '채용에 쓰는 값'(§8.4)에 나오지 않고, 이름은 계속 차지한다. 되살리는 경로는 없다.

### 3.5 employees

```sql
CREATE TABLE employees (
    id             TEXT PRIMARY KEY,                     -- emp_<hex8>. 직원 폴더 이름과 같다
    name           TEXT NOT NULL UNIQUE,
    department_id  TEXT NOT NULL REFERENCES departments(id),
    project_id     TEXT REFERENCES projects(id),         -- 지금 배치된 프로젝트. NULL = 미배치
    status         TEXT NOT NULL DEFAULT 'idle'
                   CHECK (status IN ('idle', 'working', 'blocked', 'on_leave')),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    retired_at     TEXT                                  -- 퇴사(보관). 행과 폴더는 남는다
);
CREATE INDEX employees_department ON employees(department_id);
CREATE INDEX employees_project ON employees(project_id);
CREATE UNIQUE INDEX employees_one_hr ON employees(department_id)
    WHERE department_id = 'hr' AND retired_at IS NULL;
```

- 직원은 한 번에 한 프로젝트에만 배치된다. 배치용 조인 테이블은 없다. `[§12]`
- 인사 직원은 회사에 1명이고(`employees_one_hr`) `project_id` 는 항상 NULL 이다 (코드 규칙).
- 기본 인사 직원: 서버가 시작할 때 `hr` 파트의 직원 행이 하나도 없으면(퇴사자 포함) `id = emp_68720001` 로 한 명 만든다 (H-2, §3.15). 만든 뒤에는 서버가 이름도 파일도 덮어쓰지 않는다.
- API 가 내보내는 직원 행에는 `company_wide`(bool)가 붙는다. 컬럼이 아니라 계산한 값이고, 파트가 `hr` 이면 `true` 다. 인사는 어느 프로젝트에도 배치되지 않지만, 프로젝트의 직원 목록에는 재직 중인 인사가 함께 실린다 (§6.4).
- 퇴사는 상태 값이 아니라 `retired_at` 으로 표시한다. 퇴사한 직원의 이름은 다시 쓸 수 없다. `[§12]`
- `status` 는 §4.9 의 규칙으로 유도한 값을 저장해 둔 것이다. 직접 바꿀 수 있는 것은 `on_leave` 로 들어가고 나오는 것뿐이다.
- 모델 · 노력치 · 릴레이 · MCP 는 이 테이블에 없다. `employee.yaml` 이 가진다 (§9).

### 3.6 rooms

```sql
CREATE TABLE rooms (
    id             TEXT PRIMARY KEY,                     -- room_<hex8>. 시드 행은 고정 id
    kind           TEXT NOT NULL CHECK (kind IN ('dm', 'task', 'department', 'system')),
    title          TEXT NOT NULL DEFAULT '',
    employee_id    TEXT UNIQUE REFERENCES employees(id),      -- dm: 상대 직원
    task_id        TEXT UNIQUE REFERENCES tasks(id),          -- task: 그 작업
    department_id  TEXT UNIQUE REFERENCES departments(id),    -- department: 그 파트
    owner_read_id  INTEGER NOT NULL DEFAULT 0,           -- owner 가 읽은 마지막 messages.id
    muted          INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),   -- 방별 음소거 (N-2)
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    archived_at    TEXT,
    CHECK ((kind = 'dm') = (employee_id IS NOT NULL)),
    CHECK ((kind = 'task') = (task_id IS NOT NULL)),
    CHECK ((kind = 'department') = (department_id IS NOT NULL))
);
CREATE UNIQUE INDEX rooms_one_system ON rooms(kind) WHERE kind = 'system';
```

- 직원마다 1:1 방 하나, 작업마다 작업방 하나, 파트마다 파트방 하나, 시스템 방은 전체에 하나다. `[§12]`
- 만드는 때: 1:1 방은 직원을 만들 때, 작업방은 작업이 `backlog` 에 처음 들어올 때(§4.3 의 C2 · T1), 파트방은 파트를 만들 때, 단체방은 owner 가 만들 때.
- owner 는 모든 방에 있으므로 `room_members` 에 행이 없다. 읽은 위치는 `owner_read_id` 에 둔다. `[§12]`
- 위 DDL 은 버전 1 의 문장이다. 버전 3 (§3.2) 뒤의 테이블은 `kind` 의 CHECK 가 `group` 을 더 받고, `muted` 다음에 `exchange_count INTEGER NOT NULL DEFAULT 0` 이 있다.
- 단체방(`group`)은 owner 가 직원 여럿을 초대해 만드는 방이다 (`POST /studio/rooms`, §6.4). `employee_id` · `task_id` · `department_id` 가 모두 NULL 이고 (짝 CHECK 셋이 그대로 지킨다) 수에 제한이 없다 — 같은 직원들의 방이 둘이어도 된다. 앱이 같은 요청을 다시 보내 방이 둘 생기는 것은 `client_room_id` 가 막는다 (§6.4). `title` 은 만들 때 주지 않으면 구성원 이름을 초대한 순서대로 `, ` 로 이은 것이고, 그 뒤에 구성원이 바뀌어도 따라 바뀌지 않는다 (owner 가 `rename` 으로 바꾼다).
- 단체방의 `archived_at` 은 owner 가 그 방에서 나갔다는 뜻이다 (`POST /studio/rooms/{id}/archive`). 구성원 행과 글은 그대로 남고, 방 목록에서는 `?archived=1` 일 때만 보인다. 보관한 단체방에는 owner 도 글을 쓰지 못한다 (§6.6). 되살리는 경로는 아직 없다 — 보관한 방은 읽기만 된다. 다른 종류의 방은 이 경로로 보관하지 않는다.
- 방은 지울 수 있다 (T4, §6.4): 단체방은 `DELETE /studio/rooms/{id}` 로 방 행 · 글 · 구성원 행째 지우고(`room.deleted`), 1:1 방은 `POST /studio/rooms/{id}/clear` 로 글과 구성원 행의 대화 상태(`session_id` · `conversation_id` · `fed_message_id`)만 비운다(`room.cleared`). 작업방 · 파트방 · 시스템 방은 지우지 않는다.
- `exchange_count` 는 단체방과 파트방에서 직원끼리 주고받은 턴의 수다 (`tasks.exchange_count` 와 같은 쓰임, §4.6). 직원의 멘션으로 들어온 턴이 글을 남기고 끝날 때마다 1 올리고, owner 가 그 방에 글을 쓰거나 이어 가기(`POST /studio/rooms/{id}/continue`, §6.4)를 하면 0 으로 돌린다. 값이 바뀔 때 `room.updated` 를 낸다. 1:1 방 · 작업방 · 시스템 방에서는 쓰지 않는다 (작업방은 `tasks.exchange_count` 를 쓴다).

### 3.7 room_members

```sql
CREATE TABLE room_members (
    room_id          TEXT NOT NULL REFERENCES rooms(id),
    employee_id      TEXT NOT NULL REFERENCES employees(id),
    session_id       TEXT,                               -- history.sqlite sessions.id (FK 없음)
    conversation_id  TEXT,                               -- Claude Code 대화 id (--resume). NULL = 다음 턴은 새 대화
    fed_message_id   INTEGER NOT NULL DEFAULT 0,         -- 이 대화에 이미 넣은 마지막 messages.id
    joined_at        TEXT NOT NULL,
    left_at          TEXT,
    PRIMARY KEY (room_id, employee_id)
);
CREATE INDEX room_members_employee ON room_members(employee_id);
```

- 행 하나가 전제 1의 "직원×방 대화 하나"다. 채팅 턴과 읽기 전용 턴이 이 대화를 잇는다. 대화가 길어져 새 대화로 넘길 때(S-7) `conversation_id` 를 비운다 — 끝난 턴의 입력 토큰이 `chat.HANDOFF_TOKENS`(10만. 값 잠정)를 넘었을 때와, `--resume` 이 실패해 그 턴이 새로 시작했을 때다. 다음 턴은 그 세션에서 마지막으로 `done` 으로 끝난 런의 `state` · `open_issues` · `next_steps` 를 인계(HANDOFF)로 실어 새 대화로 시작한다. 모델을 따로 불러 요약하지 않는다. `session_id` 는 직원×방에 하나이고 새 대화로 넘어가도 그대로다. `fed_message_id` 는 턴이 `done` 으로 끝났을 때 그 턴이 읽은 마지막 글의 id 로 올린다 (답이 비어도 같다, §9.5. `studio/chat.py`).
- 작업방에서 담당의 작업 런은 이 대화가 아니라 `tasks.run_id` 의 런이 가진 단계별 대화(`RunState.stage_sessions` · `done_sessions`, `pipeline.py` 200 · 216–217)를 쓴다. 릴레이는 단계마다 대화가 따로라 "직원×방 대화 하나"와 글자 그대로 같지 않다. `[§12]`
- 구성원: `dm` 은 그 직원 1행, `task` 는 담당과 owner 가 넣은 직원(§4.3 의 T3, S-9), `department` 는 그 파트의 재직 직원, `system` 은 행이 없다. `group` 은 owner 가 초대한 직원이다: 재직 중이면 인사도, 프로젝트에 배치되지 않은 직원도 된다 (퇴사한 직원은 안 된다. 휴직은 가리지 않는다 — H-3 확정, §6.4). 2명 이상으로 만들고 `Config.studio_group_max_members`(기본 8)명까지다 (§6.4).
- 단체방에서 내보낸 직원의 행은 지우지 않고 `left_at` 을 채운다. 다시 초대하면 `left_at` 을 비워 그 행 — `session_id` · `conversation_id` · `fed_message_id`, 곧 그 방에서 하던 대화 — 으로 돌아온다. 작업방의 구성원과 같은 규칙이다 (S-9). 직원이 퇴사해도 단체방의 행은 그대로 남는다 (파트방과 같다) — 퇴사한 직원과 내보낸 직원은 단체방의 턴을 받지 않는다 (§6.6 '단체방의 턴'). 파트방도 같은 턴 규칙을 쓴다 — 파트방의 구성원(그 파트의 재직 직원)이 같은 순서로 턴을 받는다. 파트방의 글쓰기(`room_post`) · 멘션 · 턴이 모두 구성원 행 기준이다 — 직원을 만드는 두 경로(`_enroll`, 채용 승인)가 행을 넣는다.
- 쓰기 권한 열은 없다. 작업방에서 쓰는 사람은 `tasks.assignee_id` 한 명이다 (S-9).

### 3.8 messages

```sql
CREATE TABLE messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id           TEXT NOT NULL REFERENCES rooms(id),
    author_kind       TEXT NOT NULL CHECK (author_kind IN ('owner', 'employee', 'system')),
    author_id         TEXT REFERENCES employees(id),
    body              TEXT NOT NULL DEFAULT '',
    mentions_json     TEXT NOT NULL DEFAULT '[]',        -- 직원 id 목록
    attachments_json  TEXT NOT NULL DEFAULT '[]',        -- [{name, path, mime, size}] (§6.6)
    meta_json         TEXT NOT NULL DEFAULT '{}',        -- 카드용 참조 {task_id, inbox_id, event, …}. 키는 §7.6
    run_id            TEXT,                              -- 이 글을 만든 런 (FK 없음)
    created_at        TEXT NOT NULL,
    CHECK ((author_kind = 'employee') = (author_id IS NOT NULL))
);
CREATE INDEX messages_room ON messages(room_id, id);
```

- 글은 고치거나 지우지 않는다.
- 본문과 첨부가 모두 비면 거부한다 (코드 검사).
- 페이지네이션은 `(room_id, id)` 로 한다 (§6).

### 3.9 campaigns

```sql
CREATE TABLE campaigns (
    id           TEXT PRIMARY KEY,                       -- cmp_<hex8>
    project_id   TEXT NOT NULL REFERENCES projects(id),
    title        TEXT NOT NULL CHECK (length(trim(title)) > 0),
    goal         TEXT NOT NULL CHECK (length(trim(goal)) > 0),
    mode         TEXT NOT NULL DEFAULT 'milestone' CHECK (mode IN ('nonstop', 'milestone', 'each')),
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'running', 'paused', 'done', 'cancelled')),
    runner_id    TEXT,                                   -- 쓰지 않는다. NULL 로 둔다 (S-14 확정. 아래)
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX campaigns_project ON campaigns(project_id, status);
```

- 캠페인 하나는 프로젝트 하나에 속한다.
- 진척률은 저장하지 않고 `tasks` 에서 센다 (S-13).
- `mode` 는 캠페인이 끝나기(`done` · `cancelled`) 전에는 언제든 바꿀 수 있고 다음 시작부터 적용된다 (S-14 확정 — `PATCH /studio/campaigns/{id}`, §4.10).
- `runner_id` 는 쓰지 않고 `NULL` 로 둔다 (S-14 확정). studio 캠페인의 상태는 이 테이블에, 진행은 줄(§4.6)에 있어 `runs/campaigns/<cid>.json` 을 만들지 않는다 (§4.10). 열은 지우지 않는다 — 지우려면 마이그레이션이 필요하다.

### 3.10 tasks

```sql
CREATE TABLE tasks (
    id                 TEXT PRIMARY KEY,                 -- task_<hex8>
    project_id         TEXT NOT NULL REFERENCES projects(id),
    campaign_id        TEXT REFERENCES campaigns(id),
    parent_id          TEXT REFERENCES tasks(id),        -- 후속 작업의 부모
    title              TEXT NOT NULL CHECK (length(trim(title)) > 0),
    reason             TEXT NOT NULL CHECK (length(trim(reason)) > 0),          -- 사유
    spec               TEXT NOT NULL CHECK (length(trim(spec)) > 0),            -- 명세
    done_criteria      TEXT NOT NULL CHECK (length(trim(done_criteria)) > 0),   -- 완료 기준
    type               TEXT NOT NULL CHECK (type IN ('feature', 'fix', 'chore', 'doc', 'data')),
    status             TEXT NOT NULL CHECK (status IN
                       ('proposed', 'backlog', 'working', 'blocked', 'review', 'done', 'rejected', 'cancelled')),
    proposer_kind      TEXT NOT NULL CHECK (proposer_kind IN ('owner', 'employee')),
    proposer_id        TEXT REFERENCES employees(id),
    assignee_id        TEXT REFERENCES employees(id),    -- 담당. 쓰기 권한을 가진 한 명
    is_milestone       INTEGER NOT NULL DEFAULT 0 CHECK (is_milestone IN (0, 1)),   -- 고비 표시
    relay              TEXT,                             -- 이 작업만의 릴레이 이름. NULL = 파트 기본값
    stage_models_json  TEXT NOT NULL DEFAULT '{}',       -- 이번만 덮어쓰는 값 {단계: {provider, model, effort}}
    run_id             TEXT UNIQUE,                      -- 지금의 작업 런 (RunState.id, FK 없음)
    queued_at          TEXT,                             -- 슬롯을 기다리기 시작한 시각 (§4.6)
    exchange_count     INTEGER NOT NULL DEFAULT 0 CHECK (exchange_count >= 0),      -- 직원 간 왕복 수
    upstream_rejected  INTEGER NOT NULL DEFAULT 0 CHECK (upstream_rejected IN (0, 1)),  -- 선행 반려 표시 (S-14)
    sort_order         INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    started_at         TEXT,                             -- 처음 working 이 된 시각
    finished_at        TEXT,                             -- done · rejected · cancelled 가 된 시각
    CHECK ((proposer_kind = 'employee') = (proposer_id IS NOT NULL)),
    CHECK (parent_id IS NULL OR parent_id <> id),
    CHECK (status NOT IN ('proposed', 'backlog', 'rejected', 'cancelled') OR run_id IS NULL),
    CHECK (status NOT IN ('working', 'blocked', 'review') OR (run_id IS NOT NULL AND assignee_id IS NOT NULL)),
    CHECK (queued_at IS NULL OR status IN ('backlog', 'working')),
    CHECK ((status IN ('done', 'rejected', 'cancelled')) = (finished_at IS NOT NULL))
);
CREATE INDEX tasks_project_status ON tasks(project_id, status);
CREATE INDEX tasks_campaign ON tasks(campaign_id, sort_order);
CREATE INDEX tasks_assignee ON tasks(assignee_id, status);
CREATE INDEX tasks_parent ON tasks(parent_id);
CREATE INDEX tasks_queue ON tasks(queued_at) WHERE queued_at IS NOT NULL;
```

- 사유 · 명세 · 완료 기준이 비면 DB 가 거부한다 (S-2). `tasks.py` 는 그 전에 같은 검사를 해 §4.5 의 오류로 돌려준다.
- `status` 는 `tasks.py` 만 바꾼다. 허용 전이는 §4.
- `stage_models_json` 은 `RelayEngine.create` 의 `stage_models` 인자 모양 그대로다 (`pipeline.py` 503, 562–580). 시작을 요청하고 줄을 서 있는 동안 서버가 재시작돼도 남도록 컬럼에 둔다.
- `run_id` 는 `working` · `blocked` · `review` · `done` 에서 값이 있고, T9(되돌리기)와 T11(`blocked` 의 취소) 때 비운다. `cancelled` 는 `rejected` 처럼 런이 없다. 지난 런 id 는 `task_events.detail_json` 에 남는다.
- 담당은 `backlog` 에서만 바꾼다 (T1 승인과 함께 정할 수도 있다). `[§12]`
- 캠페인의 작업은 그 캠페인과 `project_id` 가 같아야 한다 (코드 검사).

### 3.11 task_deps

```sql
CREATE TABLE task_deps (
    task_id        TEXT NOT NULL REFERENCES tasks(id),   -- 뒤 작업
    depends_on_id  TEXT NOT NULL REFERENCES tasks(id),   -- 선행 작업
    created_at     TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_id),
    CHECK (task_id <> depends_on_id)
);
CREATE INDEX task_deps_reverse ON task_deps(depends_on_id);
```

- 넣을 때 코드가 같은 트랜잭션에서 검사한다: 두 작업이 같은 프로젝트이고, 넣은 뒤에 순환이 없다.
- 선행 관계는 뒤 작업이 `proposed` · `backlog` 일 때만 넣고 뺀다.
- 선행이 채워졌는지의 판정은 진행 방식에 따른다 (§4.6).

### 3.12 task_events

```sql
CREATE TABLE task_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    at           TEXT NOT NULL,
    actor_kind   TEXT NOT NULL CHECK (actor_kind IN ('owner', 'employee', 'system')),
    actor_id     TEXT REFERENCES employees(id),
    kind         TEXT NOT NULL,                          -- 아래 목록. CHECK 없음 (목록이 늘어난다)
    rule         TEXT,                                   -- §4.2 의 번호 C1 · C2 · T1–T11. 전이가 아니면 NULL
    from_status  TEXT,
    to_status    TEXT,
    reason       TEXT NOT NULL DEFAULT '',               -- 왜
    detail_json  TEXT NOT NULL DEFAULT '{}',             -- run_id · inbox_id · decision 등
    CHECK ((actor_kind = 'employee') = (actor_id IS NOT NULL)),
    CHECK ((rule IS NULL) = (to_status IS NULL))
);
CREATE INDEX task_events_task ON task_events(task_id, id);
```

- 덧붙이기만 한다. 고치거나 지우지 않는다.
- `kind` 목록 (`tasks.py` 의 상수로 둔다): `created`(C1 · C2) · `transition`(T1–T11) · `assigned` · `queued` · `unqueued` · `progress`(`task_progress` 도구) · `edited` · `deps` · `run`(런 시작 · 재개 · 종료 · 건너뛴 단계, §4.7) · `limit`(한도 도달) · `flag`(선행 반려 표시) · `guard`(가드 훅이 결재 없이 거부한 명령, §4.5).
- `created` 는 `from_status` 가 NULL 이고 `to_status` 가 첫 상태다. `transition` 은 둘 다 있다.

### 3.13 inbox

```sql
CREATE TABLE inbox (
    id                TEXT PRIMARY KEY,                  -- inb_<hex8>
    kind              TEXT NOT NULL CHECK (kind IN
                      ('task_proposal', 'hire', 'design_approval', 'structure_choice', 'question',
                       'pre_approval', 'budget', 'risky_command', 'final_review')),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'decided', 'cancelled')),
    title             TEXT NOT NULL,
    project_id        TEXT REFERENCES projects(id),
    task_id           TEXT REFERENCES tasks(id),
    requester_kind    TEXT NOT NULL CHECK (requester_kind IN ('employee', 'system')),
    requester_id      TEXT REFERENCES employees(id),
    run_id            TEXT,                              -- 결정 뒤 이어 갈 런 (FK 없음)
    payload_json      TEXT NOT NULL DEFAULT '{}',        -- 종류별 모양은 §5
    decision          TEXT,                              -- 종류별 허용 값은 §5
    decision_comment  TEXT NOT NULL DEFAULT '',
    decision_json     TEXT NOT NULL DEFAULT '{}',        -- 수정 요청 등 구조가 있는 결정 내용 (§5). risky_command 승인의 1회 허용 grant 도 여기 둔다 (§10.5)
    created_at        TEXT NOT NULL,
    closed_at         TEXT,                              -- decided 또는 cancelled 가 된 시각
    reminded_at       TEXT,                              -- 마지막 재알림 (N-2)
    delivery          TEXT NOT NULL DEFAULT 'none'       -- 결정문을 run_id 의 런에 넣었는가 (아래, §5.5)
                      CHECK (delivery IN ('none', 'pending', 'delivered')),
    delivered_at      TEXT,                              -- 결정문을 넣은 시각
    CHECK ((requester_kind = 'employee') = (requester_id IS NOT NULL)),
    CHECK ((status = 'pending') = (closed_at IS NULL)),
    CHECK ((status = 'decided') = (decision IS NOT NULL)),
    CHECK ((delivery = 'delivered') = (delivered_at IS NOT NULL)),
    CHECK (delivery = 'none' OR status = 'decided')
);
CREATE INDEX inbox_status ON inbox(status, created_at);
CREATE INDEX inbox_task ON inbox(task_id);
CREATE INDEX inbox_run ON inbox(run_id) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX inbox_one_pending_per_task ON inbox(task_id)
    WHERE status = 'pending' AND task_id IS NOT NULL;
CREATE INDEX inbox_undelivered ON inbox(run_id) WHERE delivery = 'pending';
```

- 작업 하나에 `pending` 결재는 하나뿐이다 (`inbox_one_pending_per_task`). 그래서 "이 작업이 무엇을 기다리는가"가 하나로 정해진다 (§4.1). `[§12]`
- 두 번 결정 막기: `UPDATE inbox SET status = 'decided', … WHERE id = ? AND status = 'pending'` 의 바뀐 행 수가 1이 아니면 거부한다 (S-5 완료 기준).
- `hire` 는 `task_id` 가 없다. `hire_recommendation` 도 `task_id` · `run_id` 가 없다 — 작업 상태와 묶이지 않는 결재다 (§5.3). 위 DDL 은 버전 1 의 문장이고 `kind` 의 CHECK 에 이 종류를 넣은 것은 버전 5 다 (§3.2).
- `risky_command` 를 `approve` 로 결정한 행은 `decision_json.grant` 에 1회 허용을 둔다. 허용을 쓰거나 닫을 때 결정 뒤에도 이 키가 바뀐다 (§10.5).
- 저장소는 `decision_json` 을 `decision_data` 키로 주고받는다 (§3.1 의 JSON 규칙).
- `delivery` 는 결정문(결정 내용을 직원에게 전하는 지시)을 `run_id` 의 런에 넣었는지다. `none` = 런에 넣을 결정문이 없다(아직 결정 전, 취소됨, 런을 잇지 않는 결정). `pending` = 결정됐고 아직 넣지 않았다. `delivered` = 넣었다(`delivered_at`). `delivery` · `delivered_at` 은 CAMPAIGN.md 에 없는 컬럼이다. 결정별 값은 §5.5 의 표다. `[§12]`
- `delivery` 를 `pending` 으로 두는 전이는 T5 · T8 뿐이다. 넣는 때는 §4.7 — 런이 멈춰 있으면 바로, 돌고 있으면 진행 스레드가 돌아올 때다. 넣을 런은 `run_id` 이고, 찾을 때는 `inbox_undelivered` 로 `closed_at` 순으로 본다. T7 · T9 · T11 은 그 작업에 남은 `pending` 을 `none` 으로 돌린다(런을 더 잇지 않는다) (§5.5).
- 작업 상태로 미루지 않고 컬럼으로 두는 이유: 런이 도는 중에 T5 를 받았는데 그 런이 결재를 또 올리면(T4) 작업은 다시 `blocked` 가 되고, 상태만 봐서는 앞 결정문을 넣었는지 알 수 없다. 컬럼은 서버가 재시작해도 남는다(§4.8).
- 종류별 결정문 문안은 §5.6 이다. T5 의 결정 값 가운데 `pending` 으로 두지 않는 것은 없다 — 차단 6종은 결정 값이 무엇이든 `delivery = 'pending'` 이다 (§5.5).

### 3.14 devices

```sql
CREATE TABLE devices (
    id             TEXT PRIMARY KEY,                     -- dev_<hex8>
    name           TEXT NOT NULL,
    platform       TEXT NOT NULL DEFAULT 'android',
    token_hash     TEXT NOT NULL UNIQUE,                 -- 기기 토큰의 SHA-256 hex. 원문은 저장하지 않는다
    fcm_token      TEXT,
    settings_json  TEXT NOT NULL DEFAULT '{}',           -- 방해 금지 시간 {quiet_hours} (N-2, §5.9)
    created_at     TEXT NOT NULL,
    last_seen_at   TEXT,
    revoked_at     TEXT                                  -- 폐기. 이 토큰은 401
);
CREATE UNIQUE INDEX devices_fcm ON devices(fcm_token)
    WHERE fcm_token IS NOT NULL AND revoked_at IS NULL;
```

- 공용 토큰(`Config.auth_token`, `config.py` 46)은 그대로 두고 기기별 토큰을 더한다 (M-2). 인증 순서는 §6.2 다.
- 1회용 페어링 코드(5분 만료)는 테이블에 두지 않고 서버 메모리에 둔다 — `studio/devices.py` 의 `Pairings` 가 코드의 SHA-256 과 만료 시각만 쥔다. 서버가 재시작되면 코드는 무효가 된다 (M-2 가 확정했다). `[§12]`
- `fcm_token` 은 폐기되지 않은 기기 가운데 하나만 가진다 (`devices_fcm`). 같은 값으로 다시 페어링하면 앞 기기 행의 `fcm_token` 을 NULL 로 바꾸고 새 기기에 넣는다 — 앞 기기를 폐기하지는 않는다. 빈 문자열은 NULL 로 넣는다 (M-2).
- `settings_json` 은 `{quiet_hours: {start, end, utc_offset_min}}` 이다 (N-2 확정. 뜻은 §5.9 의 방해 금지). `quiet_hours` 가 없으면 방해 금지가 없다. `start` · `end` 는 `HH:MM`(00:00–23:59), `utc_offset_min` 은 -720–840 의 정수이고 생략할 수 있다. `PATCH /studio/devices/{id}` 가 `settings` 를 통째로 바꾸고(`null` 은 `{}`), 그 밖의 키는 422 다. 기기 행은 `settings` 로 내보낸다.

### 3.15 버전 1 의 시드 (그리고 버전 4 의 아트)

| 테이블 | 행 (적지 않은 컬럼은 기본값, 시각은 표 아래) |
|---|---|
| `departments` | `(id, name, builtin, default_relay, sort_order)` = `('hr', '인사', 1, NULL, 0)` · `('design_doc', '기획(문서)', 1, 'doc-write', 1)` · `('program', '프로그램', 1, 'auto', 2)` · `('design_setup', '기획(세팅)', 1, 'editor-task', 3)` |
| `rooms` | `(id, kind, title, department_id)` = `('room_system', 'system', '시스템 공지', NULL)` · `('room_dep_hr', 'department', '인사', 'hr')` · `('room_dep_design_doc', 'department', '기획(문서)', 'design_doc')` · `('room_dep_program', 'department', '프로그램', 'program')` · `('room_dep_design_setup', 'department', '기획(세팅)', 'design_setup')` |

`created_at` · `updated_at` 은 NOT NULL 이고 기본값이 없다. 시드 INSERT 는 두 컬럼에 SQL 식 `strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')` 을 쓴다. `_now()` 와 같은 형식(지역 시각, 초 단위)이고 인자가 필요 없어서 `MIGRATIONS` 를 SQL 문장 목록 그대로 둘 수 있다. 시드가 아닌 행은 저장소가 `_now()` 값을 인자로 넣는다.

```sql
INSERT INTO departments (id, name, builtin, default_relay, sort_order, created_at, updated_at)
VALUES ('hr', '인사', 1, NULL, 0,
        strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'),
        strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'));
```

직원은 마이그레이션의 시드에 넣지 않는다. 검증용 시드 직원 1명은 S-7 이 파일로 둔다 (`relay_agent/studio/seed/`, §2.2). 서버가 시작할 때 — `server.py` 의 startup 함수 `seed_studio_employee` → `api.seed_employee()` → `employees.seed()`, 저장소를 연 뒤 — `employees` 에 행이 하나도 없으면(퇴사자 포함) 한 번 만든다: 폴더 `runs/studio/employees/emp_5eed0001/` 의 파일 넷 → 한 트랜잭션에서 직원 행(`id = emp_5eed0001`, 파트 `program`, 미배치) · 1:1 방 · 그 방과 파트 방의 구성원. 행이 하나라도 있으면 아무것도 하지 않으므로 다시 켜도 중복이 없고, 시드를 퇴사시켜도 다시 생기지 않는다. 파일 쓰기나 트랜잭션이 실패하면 새로 만든 폴더를 지우고, 실패해도 서버는 뜬다. 첫 인사 글은 만들지 않는다. 첫 git 커밋(H-3)은 이 함수가 아니라 뒤에 도는 startup 함수 `commit_studio_folders` 가 만든다 (§9.6). 이름과 파일 내용은 임시값이다.

**버전 4: 아트 파트와 파트방 (T3).** `schema.py` 의 `_ART_V4` — `INSERT … SELECT` 두 문장이고 시각은 위와 같은 SQL 식이다. 버전 1 의 시드는 건드리지 않는다. ① `departments` 에 `('art', '아트', 1, 'editor-task', sort_order)` 를 넣는다. `sort_order` 는 그때의 가장 큰 값 + 1 이다 — 빈 DB 에서는 4 (다섯 번째)이고, 사용자 파트가 있는 DB 에서는 그 뒤라서 owner 가 만든 파트의 순서를 밀거나 겹치게 하지 않는다. 같은 `id` 이거나 같은 이름('아트')의 파트가 이미 있으면(보관된 것 포함) 넣지 않는다: `name` 이 UNIQUE 라 넣다 실패하면 마이그레이션 전체가 되돌아가고, owner 의 파트를 고치거나 이름을 바꾸는 것은 '덮어쓰지 않는다'에 어긋난다. ② `rooms` 에 `('room_dep_art', 'department', '아트', 'art')` 를 넣는다. 내장 `art`(`builtin = 1`)가 있고 그 방이 없을 때만이다. 이미 도는 DB 는 다음 시작에 이 한 번으로 아트를 얻고 owner 가 고친 다른 파트의 값은 건드리지 않는다. 마이그레이션은 한 번만 도므로 '아트'라는 사용자 파트가 있던 DB 에는 내장 `art` 가 생기지 않는다 — 그 파트가 아트 노릇을 하고, 내장 파트가 필요하면 owner 가 이름을 바꾼 뒤 직접 만든다. 빈 칸(`guide_template` 등)은 위의 시작 때 채움이 같은 길로 채운다.

기본 인사 직원 1명도 같은 방식으로 만든다 (H-2. 원본은 `relay_agent/studio/hr/` 이고 임시값이 아니다). `server.py` 의 startup 함수 `ensure_studio_hr` → `api.ensure_hr()` → `employees.fill_hr_part()` · `employees.ensure_hr()` 이고, `seed_studio_employee` 바로 다음에 돈다 — 빈 DB 는 시드와 인사 2명으로 시작한다. 조건은 `hr` 파트의 직원 행이 하나도 없는 것이다(퇴사자 포함. 다른 파트의 직원이 있어도 만든다): 폴더 `runs/studio/employees/emp_68720001/` 의 파일 넷(PERSONA · GUIDE · 빈 MEMORY · employee.yaml) → 한 트랜잭션에서 직원 행(`id = emp_68720001`, 파트 `hr`, `project_id` NULL) · 1:1 방 · 그 방과 `room_dep_hr` 의 구성원 · 이벤트(`employee.updated` 1개, `room.updated` 2개). 이름은 `인사` 이고, 그 이름을 이미 누가 쓰고 있으면(퇴사자 포함) `인사 2`, `인사 3` … 중 비어 있는 첫 이름이다. 한 번 만든 뒤에는 아무것도 덮어쓰지 않는다: 고친 이름과 파일은 다시 켜도 그대로이고, 인사를 퇴사시켜도 다시 만들지 않는다. 원본(`hr/GUIDE.md`)이 바뀌어도 맞추지 않는다. 대신 시작할 때 재직 중인 인사의 `GUIDE.md` 에 '채용에 쓰는 값'이라는 글자가 없으면 — 그 절을 가리키기 전의 문안이다 — 경고 한 줄을 로그에 남긴다 (`employees.hr_guides_behind`). 맞추는 것은 owner 가 손으로 한다. 행 없이 폴더만 남아 있으면 그 폴더의 파일은 그대로 쓰고 없는 파일만 채운다. 파일 쓰기나 트랜잭션이 실패하면 이번에 새로 만든 폴더를 지우고, 실패해도 서버는 뜬다(다음 시작에 다시 시도한다). 첫 인사 글은 만들지 않는다. 첫 git 커밋(H-3)은 이 함수가 아니라 뒤에 도는 startup 함수 `commit_studio_folders` 가 만든다 (§9.6).

같은 startup 이 `hr` 파트 행의 `guide_template` · `default_models_json` 도 채운다: 칸마다 비어 있을 때만(`''` · `{}`), 직원 생성과 별개 트랜잭션으로. `guide_template` 은 `hr/GUIDE.md` 의 본문이고 `default_models_json` 은 `{"chat": hr/employee.yaml 의 chat}` 이다. 값이 있는 칸은 건드리지 않고 이벤트도 내지 않는다. 일부러 비운 칸은 다음 시작에 다시 채워진다. 한쪽이 실패해도 다른 쪽은 진행한다.

나머지 네 파트(`design_doc` · `program` · `design_setup` · `art`)의 빈 칸도 시작할 때 채운다 (H-1 확정. `art` 는 T3). `server.py` 의 startup 함수 `fill_studio_parts` → `api.fill_parts()` → `employees.fill_parts()` 이고 `ensure_studio_hr` 바로 다음에 돈다. 원본은 `relay_agent/studio/parts/` 다: `guide_template` 은 `<파트 id>.md` 의 본문, `default_mcp_json` 은 `defaults.yaml` 의 그 파트 `mcp`, `default_models_json` 은 같은 파일의 `{chat, work, cap}` 이다. 규칙은 `hr` 과 같다 — 칸마다 비어 있을 때만(`''` · `[]` · `{}`) 한 트랜잭션에서 채우고, 값이 있는 칸은 건드리지 않고, (예외 하나: 원본에 '인계' 절이 있는 세 파트는 지침에 값이 있어도 그 절이 없으면 절만 '결재 기준' 앞에 덧붙인다 — H-5, `employees.with_handover`. 지침에 `## 인계` 줄이나 `handoff` 라는 말이 있으면 붙이지 않으므로 멱등이고 이벤트가 없다. 지운 절은 다시 붙는다.) 이벤트를 내지 않고, 실패해도 서버는 뜬다. 일부러 비운 칸은 다음 시작에 다시 채워지므로 네 파트의 MCP 를 빈 목록으로 둘 수는 없다. `default_relay` 는 시드의 값이고 채우지 않는다 — NULL 은 '릴레이 없음'이라는 값이다. 원본의 기본값은 §9.4 의 검사를 통과하고 `work` 의 단계 이름은 그 파트 릴레이의 것이다 (`employees.defaults_problems`, `tests/test_studio_parts.py`). `program` 의 `work` 에는 `*` 를 두지 않는다 — `auto` 가 다른 릴레이를 고르면 그 릴레이의 단계별 값이 그대로 쓰인다.

### 3.16 12개 테이블에 두지 않은 것

| 내용 | 두는 곳 | 정하는 곳 |
|---|---|---|
| WS 재접속용 단일 순번 (`after_id`) | 12개 테이블에 없다. 13번째 테이블 `stream_events` 를 마이그레이션 버전 2 로 둔다 (§7.2 가 정했다) | §7.2 `[§12]` |
| 페어링 코드 | 서버 메모리 | M-2 |
| 한도 설정값 (동시 3 · 왕복 20 · 단체방 8명 · 단체방 왕복 6 · 채용 권장 5 · 3일) | `Config`(`config.py` 17–)에 필드 추가. 단체방의 빠른 판단 모델은 `studio_group_judge_model` 이다 (T5. 기본 `haiku`, 빈 값이면 판단 없이 전원이 턴을 받는다. 판단할 때마다 읽는다, §6.6). 동시 한도의 이름은 `studio_max_runs` 로 확정했다 (S-8. 큐가 줄을 볼 때마다 읽는다). 왕복 한도의 이름은 `studio_max_exchanges` 로 확정했다 (S-9. 기본 20. `room_post` 가 멘션마다 읽는다). 단체방의 직원 상한은 `studio_group_max_members` 다 (기본 8. owner 는 세지 않는다. 방을 만들 때와 초대할 때마다 읽는다, §6.4). 단체방의 직원 간 왕복 한도는 `studio_group_max_exchanges` 다 (기본 6. 멘션으로 들어온 턴을 시작하기 직전마다 읽는다, §4.6). 채용 권장의 설정값은 둘이다: `studio_hire_backlog_limit`(기본 5. 한 직원 앞의 `backlog` 작업이 이보다 많으면 계기, 0 이하면 보지 않는다)와 `studio_hire_cooldown_days`(기본 3. 넘긴 권장과 판단 요청을 같은 키로 다시 내지 않는 날수, 0 이하면 기다림 없음). 둘 다 계기를 볼 때마다 읽는다 (§5.3) | S-8, S-9, T1 · T2 · T4 |
| 가드 감사 로그 | `runs/studio/audit/guard-<YYYYMMDD>.jsonl`. 훅이 직접 쓴다 | §10.7 |
| 런별 기록 (스킬 · MCP · 가드 경고) | `runs/studio/runlog/<run_id>.json`. 서버가 `advance()` 끝마다 덮어쓴다 | §10.7 '런별 모음' (S-11 ①) |
| 지침 이력 | 직원 폴더의 git | H-3 |
| 검수 패키지 본문 | `inbox.payload_json` (`final_review`) | §5 |

---

## 4. 작업 상태 전이

근거: CAMPAIGN.md 78–100행(상태 그림과 규칙), S-2 · S-8 · S-9 · S-12 · S-14 명세, §6 기본값 1번.

### 4.1 상태와 불변식

| 상태 | 뜻 | 반드시 참인 것 |
|---|---|---|
| `proposed` | 직원이 등록했고 owner 의 승인을 기다린다 | `pending` 인 `task_proposal` 결재가 하나 있다. 런이 없다 |
| `backlog` | 승인됐다. 아직 시작하지 않았다 | 런이 없다. `queued_at` 이 있으면 시작을 요청하고 슬롯을 기다리는 중이다 |
| `working` | 담당 직원 손에 있다 | 담당과 `run_id` 가 있다. `queued_at` 이 있으면 런을 이으려고 슬롯을 기다리는 중이다 |
| `blocked` | owner 의 결정을 기다린다. 런은 멈췄거나 곧 멈춘다 (§4.6) | `pending` 인 차단 결재(§1.5)가 하나 있다. `queued_at` 이 없다 |
| `review` | 직원이 검수를 요청했다. 런은 멈췄거나 남은 단계를 마저 돌고 있다 (§4.6) | `pending` 인 `final_review` 결재가 하나 있다 |
| `done` | owner 가 통과시켰다. 종결 | `finished_at` 이 있다 |
| `rejected` | owner 가 제안을 거절했다. 종결 | `finished_at` 이 있다. 런이 없다 |
| `cancelled` | owner 가 승인된 작업을 그만두게 했다. 종결 | `finished_at` 이 있다. 런이 없다 |

`proposed` · `blocked` · `review` 는 `pending` 결재와 1:1 로 묶인다. 결재가 결정되면 그 트랜잭션 안에서 작업이 그 상태를 떠난다. owner 가 `blocked` 작업을 취소하면(T11) 같은 트랜잭션에서 그 결재가 `cancelled` 로 닫힌다.

### 4.2 생성과 전이 한눈에

생성 2개(C1 · C2)와 전이 11개(T1–T11)가 허용되는 전부다. 세부 조건은 §4.3.

| 번호 | 전이 | 이름 | 누가 | 통로 | task_events | inbox | 런 | WS (§7.4) |
|---|---|---|---|---|---|---|---|---|
| C1 | (없음) → `proposed` | 직원 등록 | employee | MCP `task_propose` · `followup_propose` | `created` | `task_proposal` 생성 | — | `task.created` · `inbox.created` |
| C2 | (없음) → `backlog` | 내가 등록 | owner | REST | `created` | — | — | `task.created` |
| T1 | `proposed` → `backlog` | 승인 | owner | 결재 결정 | `transition` | `task_proposal` 결정 | — | `inbox.decided` · `task.transitioned` |
| T2 | `proposed` → `rejected` | 거절 | owner | 결재 결정 | `transition` | `task_proposal` 결정 | — | `inbox.decided` · `task.transitioned` |
| T3 | `backlog` → `working` | 시작 | system (큐) | 시작 요청 뒤 슬롯을 얻은 때 | `transition` | — | 런 생성 · 시작 | `task.transitioned` · `employee.status` · `run.live` |
| T4 | `working` → `blocked` | 결재 올림 | employee · system | MCP `ask_owner`, 가드 훅(§6.4), 런 종료 처리 | `transition` | 차단 결재 생성 | 런 종료 | `inbox.created` · `task.transitioned` · `employee.status` |
| T5 | `blocked` → `working` | 내가 결정 | owner | 결재 결정 | `transition` | 차단 결재 결정 | 런 재개 (슬롯 대기) | `inbox.decided` · `task.transitioned` · `employee.status` |
| T6 | `working` → `review` | 검수 요청 | employee (담당만) | MCP `task_request_review` | `transition` | `final_review` 생성 | 런 종료 | `inbox.created` · `task.transitioned` · `employee.status` |
| T7 | `review` → `done` | 통과 | owner | 결재 결정 | `transition` | `final_review` 결정 | (필요하면 `cancel`) 반영 · 커밋 | `inbox.decided` · `task.transitioned` |
| T8 | `review` → `working` | 재작업 | owner | 결재 결정 | `transition` | `final_review` 결정 | 같은 대화로 재개 (슬롯 대기) | `inbox.decided` · `task.transitioned` · `employee.status` |
| T9 | `review` → `backlog` | 되돌리기 | owner | 결재 결정 | `transition` | `final_review` 결정 | (필요하면 `cancel`) 변경 되돌림 | `inbox.decided` · `task.transitioned` |
| T10 | `backlog` → `cancelled` | 취소 | owner | REST `cancel` (§6.4) | `transition` | — | — | `task.transitioned` |
| T11 | `blocked` → `cancelled` | 취소 | owner | REST `cancel` (§6.4) | `transition` | 차단 결재 취소 | (필요하면 `cancel`) 변경 되돌림 | `inbox.cancelled` · `task.transitioned` · `employee.status` |

T9 는 CAMPAIGN.md §2 의 그림에 없는 전이다. S-12 가 "되돌리기"를 결정의 한 종류로만 두고 뒤 상태를 정하지 않아 여기서 정했다. `[§12]`

T10 · T11 도 CAMPAIGN.md §2 의 그림에 없는 전이다. owner 가 더했다 (§12.2 의 20, §12.6 의 13). 이름의 "취소"는 작업을 `cancelled` 로 보내는 것이고, 런 열의 `cancel` 은 런을 끊는 `RelayEngine.cancel` 이다 (§1.5).

### 4.3 전이별 조건과 부수 효과

모든 생성 · 전이는 `task_events` 에 누가(`actor_*`) · 언제(`at`) · 왜(`reason`)를 한 행 남긴다. 아래에는 그 밖의 것만 적는다.

**C1 직원 등록 → `proposed`**
- 언제: 호출한 직원이 재직 중이다. 사유 · 명세 · 완료 기준이 비어 있지 않다. `followup_propose` 면 `parent_id` 가 있는 작업을 가리킨다.
- 부수 효과: `proposer_kind = 'employee'`. `task_proposal` 결재를 만든다(`requester` = 그 직원). 푸시(즉시 등급, N-2). 호출한 직원의 런은 멈추지 않는다.
- 후속 작업은 전이가 아니라 부모가 연결된 새 작업이다 (CAMPAIGN.md 100행).

**C2 내가 등록 → `backlog`**
- 언제: 사유 · 명세 · 완료 기준이 비어 있지 않다.
- 부수 효과: `proposer_kind = 'owner'`. 작업방을 만든다. 결재는 없다.

**T1 `proposed` → `backlog` 승인**
- 언제: 그 작업의 `task_proposal` 결재가 `pending` 이다.
- 부수 효과: 결재를 `decided` 로. 결정에 실린 수정(제목 · 명세 · 완료 기준 · 담당)을 반영한다(`edited` · `assigned` 이벤트). 작업방을 만든다.

**T2 `proposed` → `rejected` 거절**
- 언제: T1 과 같다.
- 부수 효과: 결재를 `decided` 로. `reason` 에 거절 사유. `finished_at` 을 채운다. 다시 올리려면 새 작업을 등록한다.

**T3 `backlog` → `working` 시작**
- 계기: owner 의 시작 요청(REST, §6) 또는 캠페인 자동 진행(S-14). 직원은 계기를 만들 수 없다. 계기가 오면 조건을 검사하고 `queued_at` 을 채운다(`queued` 이벤트, `task.updated`). 이때는 아직 `backlog` 다.
- 언제 (요청 때 검사): 담당이 있다. 담당이 재직 중이고 휴직이 아니며 이 작업의 프로젝트에 배치돼 있다. 선행이 채워졌다(§4.6). 어기면 `precondition_failed`.
- 언제 (전이 순간): 큐가 이 작업에 슬롯을 줬다(§4.6). 조건은 슬롯뿐이다 — 에디터 검사는 전이 뒤에 한다(아래).
- 부수 효과: `RelayEngine.create` 로 런을 만든다(§11.2). `auto_apply = False` 를 항상 넘긴다(아래). 한 트랜잭션에서 `status = 'working'`, `run_id`, `queued_at = NULL`, `started_at`(처음일 때), 담당의 직원 상태를 바꾼다. 커밋 뒤에 런을 진행시킨다(MCP 를 쓰는 작업은 아래 에디터 검사를 먼저 한다). 담당을 작업방 구성원으로 넣는다.
- 런을 만들지 못하면(릴레이 없음, `stage_models` 오류, 서버 코드를 품은 프로젝트의 런이 원본 폴더에서 파일을 고칠 참인 경우 등. 마지막은 아래) `backlog` 에 그대로 두고 `queued_at` 을 비우며 `run` 이벤트에 오류를 남기고 시스템 방에 알린다.
- 에디터 검사 (MCP 를 쓰는 작업)는 전이 조건이 아니라 전이 뒤의 걸음이다. T3 를 커밋한 뒤, 런을 진행시키기 전에 한다(§4.5 실행 순서 3). 검사 대상은 프로젝트에 `mcp_port` 가 있고 서버 이름 `unreal` 이 런의 `mcp` · 저장소 기본 `mcp` · 릴레이의 남은 단계 `mcp` 중 하나에 있는 작업이다(`work.py` 의 `uses_editor`). 그 밖의 작업은 검사하지 않는다 — 포트가 없으면 studio 가 물어볼 곳이 없다. 에디터가 꺼져 있으면 실행하고 `health_check` 응답을 `Config.studio_editor_wait`(기본 300초)까지 2초 간격으로 기다린다(`editor.py` 의 `bring_up`, 부르는 곳은 `work.py` 의 `WorkEffects.editor_ready`). 실행할 수 없거나(엔진 · `.uproject` 없음, 실행 실패) 그 시간 안에 응답이 없으면 무응답이다. 무응답이면 런을 진행시키지 않고(`pending` 그대로) 슬롯을 놓으며, system 이 T4(`question`)를 한 뒤 큐를 다시 본다 — 작업은 `working` 이 된 직후 곧바로 `blocked` 가 된다. 기다리는 동안 owner 가 `stop` 하면 다음 확인(2초 간격) 때 기다림을 끝내고 `editor_unresponsive` 는 묻지 않는다 — 큐가 슬롯의 취소 표시를 검사에 넘기고(`editor_ready` 의 `stopped`), 결재는 `run_cancelled` 하나다(§4.7 표의 owner `stop`). T5 뒤 런을 재개하기 직전에도 같은 검사를 한다(§4.7). 조건으로 두지 않은 이유: M-7 은 무응답을 "결재로 알린다"(CAMPAIGN.md 282행)고 적는데 `backlog` 는 차단 결재를 가질 수 없다(F2). `backlog` 에 두고 시스템 방에만 알리면 푸시가 가지 않고 캠페인 자동 진행이 조용히 멈춘다. `[§12]` 두 검사의 역할: studio 의 검사는 프로젝트의 에디터 하나(배정 포트)를 켜고 기다리며, 안 되면 결재(`editor_unresponsive`)로 묻고 런을 `pending` 그대로 둔다. 엔진의 `_check_mcp_ready`(`pipeline.py` 1107–1137, 호출 1677–1679)는 런에 붙은 MCP 서버를 `mcp_probe` 로 한 번 확인할 뿐 켜지도 기다리지도 않는다. studio 의 검사를 지나도 엔진이 다시 본다 — `_check_mcp_ready` 가 실패하면 단계를 시작하지 않은 채 런을 `failed` 로 끝내고, §4.7 표의 `failed` · `working` 행이 T4 를 한다.
- 런의 워크스페이스 방식은 `create` 가 정하고 `RunState.workspace_mode` 에 남는다. 저장소 설정에서 출발하지만(`pipeline.py` 556) 엔진이 바꿀 수 있다 — 파일을 고치는 단계가 없는 릴레이는 `none`(554–555), 읽기 전용이 아닌 MCP 가 하나라도 붙으면 `copy` 가 `inplace` 이고 이벤트 `workspace_forced` 가 남는다(581–586 · 629–630). studio 는 런을 만든 뒤 이 값을 읽어 T7 · T8 · T9 · T11 의 갈래에 쓰고, 저장소 설정을 다시 읽지 않는다 (§11.2).
- 서버 코드를 품은 프로젝트의 작업 런은 원본 작업으로 만들지 않는다 — 파일을 고치는 단계가 있으면 `copy` 로만 만든다 (S-8 확정 — `create` 의 `pin_copy`. 훅이 쓰기를 막는 쪽은 S-10 확정 — `guard.write_ctx` 가 서버 루트의 `relay_agent/` 를 `protected[]` 에 넣는다. §10.8 의 8).
  - 기준: `projects.path` 를 §10.2 의 방식으로 정규화해 서버 루트의 `relay_agent/`(서버 루트는 `config.py` 14 의 `ROOT`)와 견준다. 둘이 같거나 한쪽이 다른 쪽을 품으면 "서버 코드를 품은 프로젝트"다 — 서버 루트나 그 상위 폴더를 등록한 프로젝트도, `relay_agent/` 의 하위 폴더만 등록한 프로젝트도 든다. 저장소 이름으로 가리지 않는다. 이름은 바뀔 수 있다.
  - 이 프로젝트의 작업 런은 `create` 에 `copy` 고정 인자(`pin_copy`, S-8 확정. §11.2)를 함께 넘긴다. 판정은 `studio/work.py` 의 `holds_server_code` 가 하고, `projects.path` 와 런이 실제로 도는 폴더(저장소 설정의 경로) 둘 중 하나라도 걸리면 준다. 방식이 `inplace` 로 정해졌거나(556), 파일을 고치는 단계가 있는데 `none` 으로 정해졌거나(556. 아래), `copy` 인데 읽기 전용이 아닌 MCP 때문에 `inplace` 로 바뀔 참이면(584–586) 엔진은 런을 만들지 않고 `ValueError` 를 낸다. 마지막 경우 메시지에 그 MCP 의 이름을 싣는다. 만든 뒤 `workspace_mode` 를 보고 취소하는 방식은 쓰지 않는다 — 버려지는 런이 남는다.
  - 그 뒤는 위의 "런을 만들지 못하면"과 같다. 새 오류 코드는 없다.
  - `none` 은 파일을 고치는 단계가 하나도 없을 때만 통과한다 (그런 릴레이는 554–555 가 `none` 으로 둔다). 그래도 하는 쓰기는 `relay_agent/` 에서 훅이 막는다 (§10.8). 고치는 단계가 있는데 방식이 `none` 이면 `inplace` 와 똑같이 `ValueError` 로 거부한다. 방식은 저장소 설정(`RepoSpec.workspace`, `repos.py` 34. 기본값 없음) → 릴레이 파일의 값(`RelaySpec.workspace`, `pipeline.py` 156. 적지 않으면 `none`) 순으로 그대로 받으므로(556) 고치는 단계가 있어도 `none` 이 될 수 있다. `none` 런은 `Workspace` 없이(382–384) 원본 폴더를 작업 폴더로 쓴다(1551) — 복사본도 스냅샷도 없이 원본을 고친다.
  - 걸리는 MCP 는 세 곳에서 온다 — 릴레이 단계의 `mcp`, 직원의 `employee.yaml` 의 `mcp`, 프로젝트 폴더에서 찾은 MCP (§9.5). 읽기 전용이 아닌 것이 하나라도 있으면 그 프로젝트의 작업은 그것을 뺄 때까지 시작되지 않는다. `studio` 는 `READ_ONLY_MCP` 에 더하므로 걸리지 않는다 (§11.2).
- `auto_apply` 는 저장소 설정을 따르지 않고 항상 `False` 로 넘긴다. 켜져 있으면 `copy` 런이 `done` 이 되는 순간 엔진이 검수 전에 변경을 원본에 반영하고 `changes_status` 를 `applied` 로 둔다(`pipeline.py` 1798–1803). 그러면 T7 의 반영과 T9 의 되돌리기를 엔진이 모두 거부한다(1197–1198). 인자가 `None` 일 때만 저장소 · 릴레이 설정을 보므로(587–588) 명시한 값이 이긴다 (S-8 확정). `[§12]`
- 전이가 시작 요청 때가 아니라 슬롯을 얻은 때 일어난다. 상태 8개에 "줄 선 중"이 없어서 `queued_at` 컬럼으로 표현했다. `[§12]`

**T4 `working` → `blocked` 결재 올림**

| 계기 | 누가 | 결재 종류 |
|---|---|---|
| MCP `ask_owner(kind = question)` | employee | `question` |
| MCP `ask_owner(kind = choice)` | employee | `structure_choice` |
| MCP `ask_owner(kind = approval)` | employee | `pre_approval` |
| 가드 훅이 "물어보고 진행" 목록의 명령을 잡아 서버에 물음 (`POST /studio/guard/check`, §6.4 · §10) | system | `risky_command` |
| 런이 예산 한도에 닿음 (`pipeline.py` 1583–1591, 1726–1730) | system | `budget` |
| 릴레이의 설계 게이트가 런을 멈춤 (`pipeline.py` 1783–1788) | system | `design_approval` |
| 런이 `failed` · `cancelled` 로 끝남. owner 가 런이 멈춘 줄 선 `working` 작업을 `stop` 한 것도 여기다 (§6.4) | system | `question` (오류 내용을 싣는다) |
| 런이 검수 요청 없이 `done` 으로 끝남 | system | `question` |
| 직원 간 왕복 한도를 넘음 (§4.6) | system | `question` |
| 에디터가 응답하지 않음 (M-7) — T3 직후와 런 재개 직전의 검사, 런 도중의 감지 | system | `question` |

- 언제: 작업이 `working` 이다. 슬롯을 기다리는 중(`queued_at` 이 있다)이어도 된다. `ask_owner` 는 그 작업방의 구성원인 직원이 부른다. 담당이 아닌 구성원이 불렀을 때 어느 런을 끊고 어느 런을 잇는지는 §5.7 이다 — `inbox.run_id` 는 항상 작업 런이다.
- 부수 효과: 차단 결재를 만든다(`task_id`, `run_id`, `requester`). `queued_at` 이 있었으면 비우고 `unqueued` 이벤트를 남긴다 — `blocked` 는 `queued_at` 을 가질 수 없다(§3.10 의 CHECK). 담당의 직원 상태를 다시 계산한다. 푸시(즉시 등급). 작업 런이 돌고 있었으면 진행 스레드가 돌아올 때 슬롯이 비고 큐가 다음 작업을 본다(§4.6).
- 런을 끝내는 방법: `ask_owner` 는 "이제 턴을 끝내라"를 돌려준다. 엔진이 스스로 멈추는 것은 `gate: human` 인 단계가 `needs_approval` 을 냈고 뒤 단계가 남았을 때뿐이다(`pipeline.py` 1777–1778). 그 밖에는 런이 계속 가므로, 응답을 보낸 뒤 대기 시간(`Config.studio_cut_after`, 기본 60초)이 지나도 그 런이 `running` 이면 서버가 `RelayEngine.cancel` 로 끊는다 (§8.3 의 "서버의 뒤처리". `studio/cutoff.py`). 그 전에 런이 스스로 멈췄거나 owner 가 먼저 결정했으면(T5) 건드리지 않는다. 끊긴 단계의 대화는 `RunState.stage_sessions` 에 남아 재개 때 이어진다(1606–1608). M-7 이 런 도중 무응답을 감지해 T4 를 할 때도 서버가 `cancel` 로 끊는다 — 이때는 기다리지 않고 T4 직후에 끊는다(§4.5, `work.py` 의 `editor_lost`).
- CAMPAIGN.md 의 그림은 계기를 "질문 · 구조 선택 · 사전 승인 · 위험 명령" 넷만 적는다. 아래 여섯은 여기서 더했다. 런이 실패한 작업을 `working` 에 두면 아무도 알림을 받지 못한다. `[§12]`

**T5 `blocked` → `working` 내가 결정**
- 언제: 그 작업의 차단 결재가 `pending` 이다.
- 부수 효과: 결재를 `decided` 로, `delivery = 'pending'` 으로(§3.13). `status = 'working'`, `queued_at = 지금`. 왕복 한도 때문에 생긴 결재였으면 `exchange_count = 0`. 결정문을 런에 넣는 때: 런이 멈춰 있으면 바로, 아직 돌고 있으면(T4 직후) 진행 스레드가 돌아올 때다(§4.7). 넣으면 `delivery = 'delivered'`. 큐가 슬롯을 주면 잇는다. 잇기 시작하면 `queued_at` 을 비운다(`unqueued` 이벤트).
- 결정 값이 무엇이든(승인 · 거절 · 선택지 · 답변) 전이는 T5 하나다. 결정 내용은 지시로 직원에게 간다. 런을 이을 수 없는 결정 값은 없다 — 예산 거절도 T5 와 `delivery = 'pending'` 이고 결정문만 다르다 (§5.5).

**T6 `working` → `review` 검수 요청**
- 언제: 호출한 직원이 그 작업의 담당이다. `queued_at` 이 비어 있다(런이 도는 중이다). 하네스 게이트(S-11 ②, `studio/gate.py`)를 통과한다 — 완료 기준별 검증 표기가 있고(`missing_fields`, `fields = [checklist]` — 도구의 `validate_payload` 가 본다), `feature` 유형에서 코드가 바뀌었으면 그 작업에 `choose` 로 결정된 `structure_choice` 결재가 있다(없으면 `precondition_failed`, `failed = [structure_choice_missing]`). 어기면 도구가 오류를 돌려주고 상태는 그대로다.
  - '코드가 바뀌었다'의 기준: 그 런의 편집 도구(`Edit` · `Write` · `MultiEdit` · `NotebookEdit`) `tool_use` 기록의 대상 경로(T6 때는 런이 아직 돌고 있어 `result.patch` 가 없다)와, patch 가 있으면 그 파일을 합쳐 코드 확장자(`.h .hpp .hh .inl .c .cc .cpp .cxx .cs .py .usf .ush`)가 하나라도 있는 것이다. 문서 · 설정만 바꾼 feature 는 구조 선택을 요구하지 않는다. 한계: Bash 나 에디터 MCP 로 쓴 코드와 블루프린트(`.uasset`, 바이너리)는 T6 에서 못 잡는다 — 코드는 런이 끝난 뒤 검수 패키지의 `harness_checks` 에서 `structure_choice` 가 `failed` 로 드러난다. 블루프린트(`.uasset` · `.umap`)만 바뀐 feature 는 바이너리라 구조 변경인지 값 조정인지 가릴 수 없어 거부하지 않고, `structure_choice` 를 `warning`(`data.asset_files`)으로 표시해 검수자가 본다.
- 부수 효과: 검수 패키지를 만들어 `final_review` 결재에 싣는다(§5). 담당의 직원 상태를 다시 계산한다. 푸시(즉시 등급). 도구는 "이제 턴을 끝내라"를 돌려준다. 런은 남은 단계를 마저 돌 수 있고, 슬롯은 런이 멈출 때까지 쥔다(§4.6). 캠페인이면 뒤 작업의 시작 조건을 다시 본다(§4.6).

**T7 `review` → `done` 통과**
- 언제: 그 작업의 `final_review` 결재가 `pending` 이다. 그 작업의 런이 멈춰 있다(§4.6) — T6 직후 런이 아직 돌고 있으면 `precondition_failed` 로 거부하고, owner 는 런이 멈춘 뒤 다시 결정한다. 엔진도 `done` · `cancelled` 가 아닌 런의 변경 반영 · 되돌리기를 거부한다(`_changes_action`, `pipeline.py` 1184–1186). 멈춘 런이 `failed` · `awaiting_approval` · `pending` 이면 아래 "런 정리"를 거친다.
- 런 정리 (조건 검사 뒤, 반영 · 커밋보다 먼저): 런이 `done` · `cancelled` 가 아니면 워크스페이스 방식과 상관없이 먼저 `RelayEngine.cancel` 로 `cancelled` 로 만든다(`pipeline.py` 798–805). 이유는 둘이다. 엔진이 `done` · `cancelled` 런의 변경만 반영 · 되돌린다(1184–1186). `awaiting_approval` · `pending` 런은 `cleanup_workspaces` 가 보지 않아(§11.2) 끝난 작업에 승인 대기 런이 남는다. `cancel` 이 돌려준 런이 `cancelled` 가 아니면 — studio 밖의 스레드가 그 런을 쥐고 있어 취소 표시만 남긴 경우다(792–797) — `precondition_failed` 로 거부한다. 취소 표시는 남았으므로 owner 가 잠시 뒤 다시 결정하면 된다. `failed[]` 는 `run_held` 다 (S-12 확정. `studio/review.py` 의 `close_run`).
- 부수 효과: 변경을 반영하고 git 커밋한다(푸시 없음, CAMPAIGN.md §6-5). 아래의 `copy` · `inplace` 는 저장소 설정이 아니라 그 런의 실제 방식(`RunState.workspace_mode`, `pipeline.py` 218)으로 가른다 — 엔진이 런을 만들 때 저장소 설정과 다르게 정할 수 있다 (T3). T8 · T9 의 조건도 같은 값을 본다. `copy` 는 `engine.apply_changes(run_id)` 로 원본에 반영한 뒤 커밋한다 — 바뀐 파일이 없는 런(`changes_status` 가 `none`)은 반영을 건너뛴다(`Workspace.apply` 는 빈 패치에 `WorkspaceError`, `workspace.py` 251–252). `inplace` 는 변경이 이미 폴더에 있어 엔진을 부르지 않고 커밋만 한다 — `_changes_action` 의 동작은 `apply` · `discard` · `rollback` 셋뿐이고(1201–1209) `Workspace.apply` 는 `inplace` 에서 `WorkspaceError` 다(`workspace.py` 246–247). 다른 런이 그 폴더를 원본에서 쓰는 중이면 엔진이 반영을 거부한다(1194–1196) → `precondition_failed`. 반영이나 커밋이 실패하면 전이하지 않고 결재도 `pending` 으로 둔다 — 엔진과 git 의 거부는 `engine_error`(409)로 나간다 (S-12 확정). 커밋은 `studio/review.py` 의 `commit` 이 한다: 런이 바꾼 것이 있을 때만(`changes_status` 가 `ready` · `applied`) 그 런의 폴더(`RunState.workdir`)에서 `git add -A -- .` → `git commit -- .` 을 하고, 푸시는 어디에서도 하지 않는다. `add` 뒤 커밋까지 가지 못하면(git 의 거부 · 시간 초과) 인덱스를 `add` 전으로 되돌린다 — `add` 앞에서 `git write-tree` 로 받아 둔 트리를 `git read-tree` 로 다시 읽어, 통과가 스테이징한 것은 풀리고 owner 가 미리 스테이징해 둔 것은 남는다 (트리를 못 받았으면 `git reset -- .`). 파일은 건드리지 않는다. 그 폴더 아래 전부가 한 커밋에 든다 — owner 가 그 폴더에 직접 고쳐 둔 미커밋 변경도 함께다. 폴더가 git 저장소가 아니면 커밋 없이 통과하고(런 이벤트 `commit_skipped`), 커밋했으면 `changes_committed` 에 sha 를 남긴다. 바꾼 것이 없는 런은 커밋하지 않는다 — 폴더의 무관한 변경을 쓸어 담지 않는다. `inplace` 는 커밋한 뒤 `RelayEngine.keep_changes` 로 `changes_status` 를 `applied` 로 바꾸고 스냅샷을 정리한다 (이 표시가 실패해도 전이는 간다). 실패한 뒤 할 수 있는 것은 아래 "T7 이 실패했을 때"가 정한다. 성공하면 결재를 `decided` 로. 그 작업에 남은 `delivery = 'pending'` 결재는 `none` 으로. 그 작업의 쓰지 않은 1회 허용은 닫는다 — 쓸 작업 런이 더 없다 (§10.5). `finished_at` 을 채운다. 캠페인이면 뒤 작업의 시작 조건을 다시 본다.
- T7 이 실패했을 때. 전이하지 않았고 결재는 `pending` 이다. 그 뒤 할 수 있는 것은 어디서 실패했는지에 따라 다르다.
  - (가) 반영 실패 (`copy`): 원본 · 복사본 · `changes_status`(`ready`)가 그대로다. T7 다시 · T8 · T9 를 모두 받는다. 런 정리로 `cancelled` 가 된 런은 `RESUMABLE` 이라 재작업으로 이어진다. git 이 2.38 미만이면, 원본이 그사이 바뀌어 패치가 그대로 적용되지 않는 `copy` 의 반영은 항상 실패해 여기에 든다(§11.2 의 `_merge_onto_source`).
  - (나) `copy` 반영은 됐고 커밋만 실패: 변경이 커밋되지 않은 채 원본 폴더에 있다. 런은 `changes_status = applied` · `workspace_cleaned = True` 이고 복사본과 marker 는 지워졌다(`workspace.py` 259 · 331–347, `pipeline.py` 1127 · 1136). 받는 것은 T7 다시뿐이고, 그때는 `apply_changes` 를 건너뛰고 커밋만 한다. T8 · T9 는 `precondition_failed` 로 거부한다. 런을 재개하는 것 자체는 된다 — 재개 때 `prepare` 를 다시 해(1652–1664) 반영된 원본을 새 스냅샷으로 뜬다. 그러나 엔진이 그 런의 새 변경을 모으지 않고(1094) `apply` · `discard` 도 거부한다(1197–1198 · 1190–1191). `applied` 와 `workspace_cleaned` 를 되돌리는 코드는 없다. 커밋이 계속 실패하면 owner 가 원본 폴더에서 원인을 고치거나 직접 커밋한 뒤 T7 을 다시 한다 — 커밋할 것이 없으면 성공이다(§4.5). 반영을 무르는 일은 studio 밖에서 git 으로 한다. 새 런으로 재작업을 여는 길은 두지 않는다 — 재작업은 같은 런을 다시 여는 것이고(§4.7 의 `done` 행), 변경을 결정한 런은 다시 열지 않는다. 통과시킨 뒤 더 고칠 것은 후속 작업(`followup_propose`)으로 한다 (S-12 확정).
  - (다) `inplace` 커밋 실패: 엔진을 부르지 않았으므로 런의 상태가 바뀌지 않았다. (가)와 같다. 커밋이 된 뒤에야 `keep_changes` 가 `applied` 로 바꾸므로, 커밋한 뒤의 재작업 · 되돌리기는 (나)와 같이 `precondition_failed` 다 (S-12 확정).

**T8 `review` → `working` 재작업**
- 언제: 그 작업의 `final_review` 결재가 `pending` 이면 된다. 런이 아직 돌고 있어도 받는다. 예외: 런의 변경이 이미 원본에 반영됐으면(`changes_status = applied` — `copy` 는 반영한 뒤, `inplace` 는 커밋한 뒤) `precondition_failed` 다 (T7 의 (나)). 되돌린 뒤(`discarded` · `rolled_back`)에도 거부한다 (§4.5).
- 부수 효과: 결재를 `decided` 로, `delivery = 'pending'` 으로(§3.13). `reason` 에 코멘트. `status = 'working'`, `queued_at = 지금`. 결정문(코멘트)을 런에 넣는 때는 T5 와 같다 — 런이 멈춰 있으면 바로, 아직 돌고 있으면 진행 스레드가 돌아올 때(§4.7). 넣으면 `delivery = 'delivered'`. 런이 `failed` · `cancelled` · `awaiting_approval` 로 멈춰 있으면 슬롯을 얻은 뒤 같은 대화로 이어진다. `done` 으로 끝난 런은 같은 런을 다시 열어(`RelayEngine.reopen`) 결정문을 넣고, 슬롯을 얻은 뒤 끝낸 대화를 이어 간다 — 새 런을 만들지 않는다 (§4.7 의 `done` 행. S-12 확정). 이미 시작된 뒤 작업에는 `upstream_rejected = 1` 과 알림 (S-14 확정. T9 도 같다): 이 작업을 직접 기다리는 작업(`task_deps`) 가운데 `working` · `blocked` · `review` 인 것마다, 전이와 같은 트랜잭션에서 표시를 세우고 `flag` 이벤트(system. `detail_json` 은 `{upstream_task_id, rule}`)와 `task.updated` 를 남긴다 (`TaskMachine._flag_later`). 그 작업방의 글은 결정 뒤에 결재 쪽이 쓴다 (§5.9 "선행 반려"). `backlog` 에서 기다리기만 한 뒤 작업과 간접으로 기다리는 작업에는 세우지 않는다. 표시를 내리는 때 (S-14 확정): 표시된 작업 자신이 `working` · `blocked` · `review` 를 벗어나는 전이(T7 · T9 · T11)가 같은 `UPDATE` 로 `upstream_rejected = 0` 을 쓰고, 그 전이 행의 `detail_json` 에 `upstream_rejected: false` 를 남긴다. 따로 이벤트 행이나 방의 글은 만들지 않는다. 그 세 상태에 머무는 동안(T4 · T5 · T6 · T8)에는 서 있고, 반려됐던 선행이 다시 통과해도 내리지 않는다 — 그때가 바로 뒤 작업을 다시 볼 때다.

**T9 `review` → `backlog` 되돌리기**
- 언제: T7 과 같고(결재가 `pending` 이고 런이 멈춰 있다), 아래 둘이 모두 참이다. 아니면 `precondition_failed` 로 거부하고 재작업(T8)을 안내한다. 둘이 모두 참이면 되돌리기 전에 T7 과 같은 런 정리를 한다 — 런이 `done` · `cancelled` 가 아니면 `RelayEngine.cancel`, 그래도 `cancelled` 가 아니면 `precondition_failed`.
  1. 그 런의 변경을 되돌릴 수 있다 — `inplace` 는 스냅샷이 남아 있고(`Workspace.rollback`, `workspace.py` 319–329), `copy` 는 변경을 아직 원본에 반영하지 않았다(`Workspace.discard`, 311–317). 두 방식 모두 워크스페이스가 정리됐으면(`workspace_cleaned`) 되돌릴 수 없다 — 엔진이 정리된 런의 `discard` · `rollback` 을 거부한다(`_changes_action`). `review` 에 머무는 작업의 런은 보존 정리에서 빠지므로(이 항목의 '스냅샷이 지워지는 때') 드문 경우다 (S-12 확정). 부르는 엔진 함수는 `rollback_changes` · `discard_changes`(`pipeline.py` 1222–1226)이고, 런이 `done` · `cancelled` 일 때만 받는다(1184–1186). 그래서 `failed` · `awaiting_approval` · `pending` 런은 먼저 `cancel` 한다(위). 다른 런이 그 폴더를 원본에서 쓰는 중이면 엔진이 거부한다(1194–1196). 예외: `changes_status` 가 `none` 이면 되돌릴 변경이 없다 — 엔진을 부르지 않고, 스냅샷 유무와 조건 2를 보지 않는다.
  2. `inplace` 일 때, 이 작업이 처음 `working` 이 된 뒤로 같은 프로젝트의 다른 작업이 `working` 으로 들어간 적이 없다(`task_events` 의 id 로 비교). `rollback` 은 폴더 전체를 스냅샷으로 맞추므로 다른 작업의 변경까지 지운다.
- 부수 효과: 변경을 되돌린다. 결재를 `decided` 로. `run_id = NULL`(지난 id 는 이벤트에 남긴다), `queued_at = NULL`, `exchange_count = 0`. 그 작업에 남은 `delivery = 'pending'` 결재는 `none` 으로 — 되돌린 런에는 결정문을 넣지 않는다. 그 작업의 쓰지 않은 1회 허용도 버린다 (§5.5. 닫는 방법은 §10.5). 담당은 그대로 둔다. 뒤 작업에는 T8 과 같은 표시.
- 스냅샷이 지워지는 때: 런이 멈출 때는 지워지지 않는다(`_settle_workspace`, `pipeline.py` 1168–1180 는 변경을 모으기만 한다). 예외는 하나다 — `done` 이면서 `changes_status` 가 `none` 인 런은 끝나는 순간 엔진이 워크스페이스를 정리한다(1807–1812). 되돌릴 변경이 없으므로 조건 1의 예외로 받는다. `cleanup_workspaces`(857–886)가 지운다 — 변경을 결정한 런은 바로, 결정하지 않은 `done` · `failed` · `cancelled` 런은 `run.json` 을 마지막으로 쓴 뒤 `workspace_retention_days`(`config.py` 33, 기본 3일)가 지나면. `review` 에 머무는 작업의 런은 예외다 (이 항목의 끝). `copy` 의 통과(T7)는 정리된 뒤에도 남은 `result.patch` 로 반영하지만(`pipeline.py` 1190–1191), 원본 폴더가 스냅샷 그대로일 때만 된다(`workspace.py` 255–257). 검수 중인 작업의 스냅샷은 보존 정리에서 뺀다 (S-12 확정): `RelayEngine.held_runs` 가 `review` · `blocked` 상태 작업의 `run_id` 를 주고(`blocked` 는 T11 의 되돌리기를 위해서다), `cleanup_workspaces` 는 그 런을 기간 만료로 지우지 않는다. 변경을 결정한 런은 전과 같이 바로 지운다. 목록을 읽지 못한 회차에는 기간 만료 정리를 통째로 건너뛴다 — 지우지 않는 쪽이다. 서버는 열린 studio 스토어에서(`api.held_runs`), `relay cleanup`(CLI)은 studio DB 를 읽기 전용으로 열어 읽는다(`studio/review.py` 의 `held_runs_ro`). 그래서 검수가 길어지면 그 런의 스냅샷은 결정될 때까지 디스크에 남는다.

**T10 `backlog` → `cancelled` 취소**
- 언제: owner 가 `cancel` 을 불렀다(§6.4). 줄 서 있어도(`queued_at` 이 있다) 받는다. `backlog` 에는 런도 `pending` 결재도 없어(§4.1) 되돌릴 변경과 닫을 결재가 없다 — 그 밖의 조건은 없다.
- 부수 효과: `queued_at = NULL`(`unqueued` 행은 따로 남기지 않는다), `finished_at` 을 채운다. `reason` 에 owner 가 적은 사유. 담당 · 선행 관계 · 작업방은 그대로 둔다. 캠페인이면 뒤 작업의 시작 조건을 다시 본다 — `cancelled` 인 선행은 채워지지 않는다(§4.6).
- 큐가 먼저 슬롯을 줘 T3 가 됐으면 `illegal_transition`(F10)이고, 검사와 `UPDATE` 사이에 그렇게 됐으면 `stale_state` 다. 그때는 `stop` 을 거쳐 T11 로 간다.

**T11 `blocked` → `cancelled` 취소**
- 언제: owner 가 `cancel` 을 불렀다(§6.4). 그 작업의 차단 결재가 `pending` 이다. 그 작업의 런이 멈춰 있다(§4.6) — T4 직후 런이 아직 돌고 있으면 `precondition_failed` 로 거부하고, owner 는 런이 멈춘 뒤 다시 부른다. 그리고 T9 의 조건 1 · 2 가 모두 참이다 (`changes_status` 가 `none` 이면 T9 의 예외대로 둘을 보지 않는다). 못 채우면 `precondition_failed` 로 거부하고, 결재를 결정해(T5) 작업을 이어 가는 길을 안내한다.
- 변경을 원본에 남긴 채 취소하는 길은 두지 않는다 — 예외는 런이 엔진에서 사라진 작업 하나다 (아래 `failed[]` 표 다음의 "런이 엔진에서 사라진 작업"). 남기면 주인 없는 변경이 같은 폴더의 다음 작업의 커밋(`inplace` 의 T7)에 섞인다. 그래서 스냅샷이 지워진 `inplace` 런(T9 의 "스냅샷이 지워지는 때" — 끊긴 뒤 3일이 지난 `failed` · `cancelled` 런)이나 조건 2 를 못 채우는 작업은 취소할 수 없고, T5 로 이어 검수까지 간다. 스냅샷은 T9 와 같은 방법으로 지킨다 — `held_runs` 가 `blocked` 작업의 런도 주어, 결재를 오래 기다린 작업의 스냅샷이 기간 만료로 지워지지 않는다 (S-12 확정).
- 런 정리와 되돌리기: T9 와 같은 순서다(§4.5 실행 순서의 예외). 조건을 검사한 뒤, 런이 `done` · `cancelled` 가 아니면 `RelayEngine.cancel` 로 `cancelled` 로 만들고(그래도 아니면 `precondition_failed`), `inplace` 는 `rollback_changes`, `copy` 는 `discard_changes` 로 되돌린다. 이 `cancel` 은 런을 끊는 엔진 호출이고 작업 취소가 아니다(§1.5).
- 부수 효과: 차단 결재를 `cancelled` 로 닫는다 — `closed_at` 을 채우고 `decision` 은 NULL, `delivery` 는 `none` 이다(§3.13, §5.1). `run_id = NULL`(지난 id 는 이벤트에 남긴다), `finished_at` 을 채운다. `reason` 에 owner 가 적은 사유, `detail_json` 에 닫은 결재의 `inbox_id` 와 지난 `run_id`. 그 작업에 남은 `delivery = 'pending'` 결재는 `none` 으로 — 버린 런에는 결정문을 넣지 않는다. 그 작업의 쓰지 않은 1회 허용도 버린다 (닫는 방법은 §10.5). 담당의 직원 상태를 다시 계산한다(§4.9). 캠페인이면 뒤 작업의 시작 조건을 다시 본다(§4.6).
- 결재 결정(T5)과 동시에 오면 하나만 된다. 결정이 먼저면 취소는 `illegal_transition`(F10) 또는 `stale_state`, 취소가 먼저면 결정은 `already_decided` 다(§5.1).

**T5 · T7 · T8 · T9 · T11 의 `failed[]` (S-12 확정).** 위 전이가 그 작업의 런과 변경의 상태 때문에 거부될 때의 코드다. 판정은 `studio/review.py` 의 `gate` 하나이고, 검수 패키지의 `can`(§5.4)과 결정 때의 검사(`studio/work.py` 의 `WorkEffects.check` · `before`)가 함께 쓴다. T11 은 되돌리기의, T5 는 재작업의 조건을 본다.

| 코드 | 무엇에 걸렸나 | 걸리는 전이 |
|---|---|---|
| `run_gone` | 엔진이 그 작업의 런을 읽지 못한다. 런이 엔진에서 사라졌으면 T9 · T11 은 거부하지 않는다 (표 아래) | T7 · T9 · T11 |
| `run_running` | 런이 아직 돌고 있거나 그 작업이 슬롯을 쥐고 있다 (§4.6) | T7 · T9 · T11 |
| `run_held` | 런 정리의 `cancel` 뒤에도 런이 `done` · `cancelled` 가 아니다 | T7 · T9 · T11 |
| `changes_applied` | 변경을 이미 원본에 반영했다(`applied`) — 그 뒤의 재작업 · 되돌리기 · 취소 | T5 · T8 · T9 · T11 |
| `changes_reverted` | 변경을 이미 되돌렸다(`discarded` · `rolled_back`) — 그 뒤의 통과 · 재작업 | T5 · T7 · T8 |
| `snapshot_gone` | T9 의 조건 1 — 되돌릴 스냅샷이나 복사본이 없다 | T9 · T11 |
| `folder_busy` | 다른 런이 그 폴더를 원본에서 쓰는 중이다 (변경이 `ready` 일 때, 그리고 `applied` 인 채 통과를 다시 할 때 — 그 통과도 폴더를 커밋한다) | T7 · T9 · T11 |
| `others_started` | T9 의 조건 2 | T9 · T11 |

같은 결정을 다시 하는 것(반영한 뒤의 통과, 되돌린 뒤의 되돌리기 · 취소)은 거부하지 않는다 (§4.5). T5 · T8 은 런을 읽지 못해도 받는다 — 결정문은 `delivery = 'pending'` 으로 남는다.

**런이 엔진에서 사라진 작업 (S-12 확정, `run_gone` 의 예외).** 그 런의 폴더에 `run.json` 이 없으면(`RelayEngine.load` 가 `FileNotFoundError` 를 낸다 — `studio/review.py` 의 `lost`) 엔진은 그 런의 변경을 반영할 수도 되돌릴 수도 없다. 그대로 거부하면 작업이 `review` · `blocked` 에서 나갈 길이 없으므로, T9 · T11 은 런 정리와 되돌리기를 건너뛰고 받는다. 폴더에 남은 변경은 그대로다 — "변경을 원본에 남긴 채 취소하는 길은 두지 않는다"(T11)의 하나뿐인 예외다. 흔적은 전이 행의 `reason` 끝에 남긴다: owner 가 적은 사유(없으면 기본 사유) 뒤에 `review.LOST` 문구("런이 엔진에 없어 변경을 되돌리지 못했다 — 폴더에 남은 변경은 직접 확인한다")가 붙고, owner 가 그 폴더를 직접 확인한다. T7 은 그때도 `run_gone` 으로 거부한다 — 무엇을 커밋하는지 알 수 없다. `run.json` 이 있는데 읽지 못하는 런(잠김 · 깨짐)과 엔진이 연결되지 않은 경우는 T9 · T11 도 계속 거부한다 — 스냅샷이 남아 있을 수 있는데 되돌리지 않은 채 전이하면 안 된다. 검수 패키지의 `can`(§5.4)도 같은 판정이다: `revert` 는 `ok`, `pass` 는 막힌다.

### 4.4 64칸 분류

행이 지금 상태, 열이 가려는 상태다. T 는 허용(§4.2), F 는 금지(§4.5). 허용 11칸, 금지 53칸.

| 지금 ＼ 다음 | proposed | backlog | working | blocked | review | done | rejected | cancelled |
|---|---|---|---|---|---|---|---|---|
| **proposed** | F0 | T1 | F1 | F1 | F1 | F1 | T2 | F10 |
| **backlog** | F6 | F0 | T3 | F2 | F2 | F2 | F5 | T10 |
| **working** | F6 | F7 | F0 | T4 | T6 | F3 | F5 | F10 |
| **blocked** | F6 | F7 | T5 | F0 | F4 | F3 | F5 | T11 |
| **review** | F6 | T9 | T8 | F8 | F0 | T7 | F5 | F10 |
| **done** | F9 | F9 | F9 | F9 | F9 | F0 | F9 | F9 |
| **rejected** | F9 | F9 | F9 | F9 | F9 | F9 | F0 | F9 |
| **cancelled** | F9 | F9 | F9 | F9 | F9 | F9 | F9 | F0 |

### 4.5 금지 전이와 오류

**상태 쌍으로 금지** (오류 코드 `illegal_transition`)

| 번호 | 칸 수 | 금지 | 이유 |
|---|---|---|---|
| F0 | 8 | 같은 상태로 | 전이가 아니다 |
| F1 | 4 | `proposed` → `working` · `blocked` · `review` · `done` | 직원이 등록한 작업은 내 승인을 거친다 (S-2) |
| F2 | 3 | `backlog` → `blocked` · `review` · `done` | 런 없이 건너뛸 수 없다 |
| F3 | 2 | `working` · `blocked` → `done` | 자동 완료 금지. 반드시 `review` 를 거친다 (CAMPAIGN.md 100행) |
| F4 | 1 | `blocked` → `review` | 결정을 받고 `working` 으로 돌아온 뒤에 요청한다 |
| F5 | 4 | `backlog` · `working` · `blocked` · `review` → `rejected` | `rejected` 는 제안 거절 전용이다. 승인된 작업을 그만두는 종결은 `cancelled` 이고 `backlog` · `blocked` 에서만 간다 (T10 · T11). 취소 전이는 CAMPAIGN.md 에 없다 `[§12]` |
| F6 | 4 | `backlog` · `working` · `blocked` · `review` → `proposed` | 승인을 무를 수 없다 |
| F7 | 2 | `working` · `blocked` → `backlog` | 시작을 물러 `backlog` 로 돌리는 길은 검수의 되돌리기(T9)뿐이다. 그만두는 것은 무르기가 아니라 취소다 — `blocked` 는 T11, `working` 은 `stop` 을 거쳐 T11 |
| F8 | 1 | `review` → `blocked` | 작업당 `pending` 결재는 하나다(§3.13). `review` 에는 `final_review` 가 `pending` 이라 차단 결재를 만들 수 없다. 런이 아직 도는 동안의 처리는 표 아래 |
| F9 | 21 | `done` · `rejected` · `cancelled` → 모든 다른 상태 | 종결. 이어서 할 일은 부모를 연결한 새 작업으로 만든다. 취소한 작업도 되살리지 않는다 |
| F10 | 3 | `proposed` · `working` · `review` → `cancelled` | 취소는 런이 돌지 않는 `backlog` · `blocked` 에서만 받는다. `proposed` 는 거절(T2)이 그 자리다. `working` 은 `stop` 으로 `blocked` 가 된 뒤(§6.4), `review` 는 되돌리기(T9)로 `backlog` 가 된 뒤 취소한다 |

칸 수를 더하면 53 이고 §4.4 의 금지 칸 수와 같다.

**런이 아직 도는 구간 (T4 · T6 직후).** 작업은 이미 `blocked` · `review` 인데 그 런이 남은 턴 · 단계를 도는 동안(§4.6 "슬롯") 같은 런에서 T4 의 계기가 또 올 수 있다. 차단 결재를 하나 더 만들 수 없으므로(`inbox_one_pending_per_task`) 아래처럼 처리한다. 작업 상태와 결재는 바뀌지 않는다.

| 계기 | 처리 |
|---|---|
| `ask_owner` | 결재를 만들지 않는다. `illegal_transition`(`blocked` 면 F0, `review` 면 F8) 오류와 "이미 결재를 기다리고 있다. 턴을 끝내라"를 돌려준다 |
| 가드 훅이 "물어보고 진행" 목록의 명령을 잡음 (§6.4 가드의 분기 3, §10) | 결재 없이 그 명령을 거부한다. `task_events` 에 `guard` 행을 남긴다 — `actor_kind = 'system'`, `rule` · `to_status` 는 NULL, `reason` 에 거부한 이유, `detail_json` 에 `run_id` 와 명령. `review` 면 검수 패키지에 싣는다 (§5) |
| 예산 한도 · 설계 게이트 · 런 종료 | 그 자리에서는 아무것도 하지 않는다. 진행 스레드가 돌아올 때 §4.7 의 표가 처리하고, T4 를 다시 하지 않는다 |
| 직원 간 왕복 한도에 닿음 (§4.6) | 결재를 만들지 않는다. 턴을 넣지 않고 `exchange_count` 도 올리지 않는다. `room_post` 에 오류와 "왕복 한도에 닿았다. 턴을 끝내라"를 돌려준다(오류 코드는 §8). `task_events` 에 `limit` 행을 남긴다 — 위 `guard` 행과 같은 형식이고 `detail_json` 에는 `run_id` 와 멘션 대상을 적는다. `review` 면 검수 패키지에 싣는다 (§5). 런이 멈춘 뒤에도 작업이 `working` 이 아닌 동안은 같다 |
| 에디터 무응답을 감지 (M-7) | 서버가 그 런을 `RelayEngine.cancel` 로 끊고 `task_events` 에 `run` 행(`detail = {run_id, event: 'editor_unresponsive'}`)을 남긴다. 감지는 `editor_watch.py` 가 한다 — 에디터 검사를 통과한 런을 30초마다 `health_check` 로 보고 3번 연속 응답이 없으면 무응답이다(한 번으로 끊으면 긴 MCP 명령으로 잠시 막힌 정상 에디터의 런까지 끊는다). 작업이 `working` 이고 줄 밖이면 끊기 전에 system 이 T4(`question`, `editor_unresponsive`)를 먼저 한다 — 끊고 나서 하면 진행 스레드가 돌아와 §4.7 표의 `cancelled` · `working` 행이 `run_cancelled` 결재를 하나 더 만든다. `blocked` · `review` · 줄에 선 `working` 은 결재를 만들지 않고 끊기와 `run` 행만 한다: `blocked` 와 줄에 선 `working` 은 런을 재개하기 직전의 검사(§4.3 T3)에서 다시 걸려 그때 T4 가 되고, `review` 는 그 `run` 행이 검수 패키지의 `incidents` 에 실린다 (§5). 진행 스레드가 돌아오면 §4.7 표의 `cancelled` 행을 따른다. 끊으려는 때 런이 이미 끝나 있으면 끊지 않는다(`cancel` 이 `ValueError` 를 낸다) — 먼저 한 T4 는 그대로 두고 `run` 행에 '런은 끊기 전에 이미 끝나 있었다'고 적는다(`detail` 은 같다). 그 결재의 `retry` 는 평소 길이다 — 에디터를 다시 검사하고 그 런을 잇는다. 감시는 메모리에만 있어 서버가 멈추면 사라진다 — 그 뒤는 재개 직전의 검사가 본다 |

- system 이 `review` 를 `working` 으로 되돌려 다시 묻는 길은 없다 (A3). 그래서 `review` 에서 잡힌 위험 명령은 실행되지 않은 채 owner 의 검수로 넘어간다.
- `blocked` 에서 거부된 명령은 owner 의 결정(T5)으로 런이 이어진 뒤 직원이 다시 시도하면 그때 T4(`risky_command`)가 된다.
- `blocked` · `review` 에서 왕복 한도로 거부된 멘션은 세지 않았으므로 `exchange_count` 가 그대로다. T5 · T8 로 작업이 `working` 이 된 뒤 다음 멘션에서 T4(`question`, `limit`)가 된다. 결재를 미뤄 두었다가 자동으로 만들지 않는 것은 기억할 상태가 하나 더 들어서다.
- 에디터 무응답 때 결재 없이 런을 끊는 이유: 멈춘 MCP 를 기다리는 런이 슬롯을 계속 쥐면 같은 프로젝트의 다른 작업이 줄에서 나가지 못한다(§4.6). 알림은 이미 있는 결재(`blocked`)나 검수 패키지(`review`)로 간다.
- 작업이 `working` 이고 줄 서 있는 경우(런이 도는 중에 T5 · T8 을 받았다)는 여기 들지 않는다. T4 가 그대로 일어나고, 앞 결정문은 `delivery = 'pending'` 으로 남는다 (§4.7).
- 가드의 "거부 + 기록"은 여기 둔 최소 규칙이다. 훅이 서버에 묻는 통로와 서버의 분기는 §6.4 의 가드 소절이다. 훅의 응답 형식은 §10.6, 목록은 §10.3 · §10.4 다. §10 은 이 규칙을 바꾸지 않았다.

**주체로 금지** (오류 코드 `actor_not_allowed`)

§4.2 의 "누가" 열에 없는 주체는 모두 `actor_not_allowed` 다. 예를 들어 system 의 T5, owner 의 T4 도 금지다. 아래 표는 그중 이유를 적어 둘 것만 뽑았다.

| 번호 | 금지 | 이유 |
|---|---|---|
| A1 | employee 가 T1 · T2 · T5 · T7 · T8 · T9 · T10 · T11 을 하거나 T3 의 시작을 요청 | owner 의 결정이다. "직원 주체는 `done` 전이 불가"(S-2)가 여기 든다 |
| A2 | 담당이 아닌 직원, owner, system 이 T6 | 검수 요청은 담당 직원의 도구 호출로만 생긴다 |
| A3 | system 이 T1 · T2 · T7 · T8 · T9 · T10 · T11 | 자동 승인 · 자동 완료 · 자동 취소는 없다. 캠페인 자동 진행(S-14)과 런의 승인 방식 `auto` 도 예외가 아니다 |
| A4 | owner 가 C1, employee 가 C2 | 직원이 만들면 `proposed` 부터, 내가 만들면 `backlog` 부터다 |

**오류 형식.** `tasks.py` 는 `TransitionError` 하나를 던지고 `code` 로 구분한다. HTTP 상태와 MCP 오류로 옮기는 표는 §6.3 · §8.3 이다. 코드별로 덧붙는 필드(`failed[]` · `fields[]` · `from` · `to`)의 이름과 모양은 §6.3 이 정한다.

| `code` | 뜻 |
|---|---|
| `illegal_transition` | 상태 쌍이 §4.4 에서 F 다 |
| `actor_not_allowed` | 상태 쌍은 허용이지만 이 주체는 할 수 없다 |
| `precondition_failed` | §4.3 의 "언제"를 채우지 못했다. 메시지에 무엇이 빠졌는지 적는다 |
| `missing_fields` | 생성할 때 사유 · 명세 · 완료 기준 중 빈 것이 있다 |
| `stale_state` | 검사한 뒤에 다른 요청이 상태를 먼저 바꿨다 |

**실행 순서.** 전이 하나는 이 순서로 처리한다.

1. `transaction()` 안에서: 조건 검사 → `UPDATE tasks SET status = ?, … WHERE id = ? AND status = ?`(바뀐 행이 0이면 `stale_state`) → `task_events` 행 → 결재 생성 또는 결정 → 직원 상태 갱신. 하나라도 실패하면 전부 되돌린다.
2. 커밋 뒤: WS 이벤트를 낸다.
3. 그 뒤: 밖으로 나가는 일(런 시작 · 재개, 푸시).

예외 둘: T3 는 `run_id` 가 필요해 런을 먼저 만들고(아직 `pending`) 1을 한 뒤 진행시킨다 — 1이 실패하면 만든 런을 `RelayEngine.cancel` 로 닫는다. T7 · T9 · T11 은 1보다 먼저 이 순서로 한다: 조건 검사 → 런 정리(`cancel`, 필요할 때만 — §4.3 T7) → 반영(`copy` 의 T7) 또는 되돌리기(T9 · T11) → 커밋(T7). 어느 걸음이든 실패하면 전이하지 않는다. `cancel` 까지만 된 경우 런은 `cancelled` 로 남고, owner 는 같은 결정을 다시 하거나 재작업(T8)을 할 수 있다. 다 됐는데 1이 실패하면 작업은 `review`, 결재는 `pending` 으로 남는다. owner 가 같은 결정을 다시 하면 커밋할 변경이 없거나 이미 되돌려져 있어도 성공으로 보고 1만 다시 한다. `copy` 가 `applied` 인데 커밋이 안 됐으면 반영을 건너뛰고 커밋부터 다시 한다(§4.3 T7 의 (나)). 이미 한 것과 다른 결정(커밋한 뒤의 되돌리기, `copy` 를 반영한 뒤의 재작업 · 되돌리기, 되돌린 뒤의 통과 · 재작업)은 `precondition_failed` 로 거부한다. 이미 했는지는 `RunState.changes_status`(`pipeline.py` 220)로 본다 — 엔진도 이미 결정된 변경(`applied` · `discarded` · `rolled_back`)에는 다시 하기를 거부한다(1197–1198). `inplace` 의 T7 은 커밋한 뒤 `RelayEngine.keep_changes` 가 `changes_status` 를 `applied` 로 바꾸므로 git 상태를 읽지 않는다 (S-12 확정). 판정은 `studio/review.py` 의 `gate` 하나이고(코드는 §4.3 의 `failed[]` 표), 조건 검사 때 한 번, 런 정리 직전에 작업별 잠금 안에서 한 번 더 한다 — 함께 온 통과와 되돌리기가 둘 다 손대지 않는다. T11 은 결재 결정이 아니라 `cancel` 요청이지만 같은 규칙이다 — `cancel` 까지만 됐거나 다 됐는데 1이 실패하면 작업은 `blocked`, 결재는 `pending` 으로 남고, owner 가 다시 취소하면 이미 되돌려져 있어도 성공으로 보고 1만 다시 한다. 되돌린 뒤 그 결재를 결정해 잇는 것(T5)은 `precondition_failed`(`changes_reverted`)로 거부한다 — 되돌린 런은 이을 수 없으므로 취소를 다시 한다 (S-12 확정).

### 4.6 큐와 한도

한도는 CAMPAIGN.md §6 의 1번이다.

| 한도 | 기본값 | 세는 것 | 걸리는 지점 |
|---|---|---|---|
| 전체 동시 실행 | 3 (설정) | 슬롯을 쥔 작업 런 수 (아래 "슬롯") | T3 의 전이 순간, T5 · T8 의 런 재개 |
| 같은 프로젝트 쓰기 | 1 (고정) | 같은 `project_id` 에서 슬롯을 쥔 작업 런 수 | 위와 같다 |
| 직원 간 왕복 | 20 (설정) | `tasks.exchange_count` | 작업방에서 직원이 다른 직원을 멘션해 턴을 넣을 때 (S-9) |
| 단체방 · 파트방의 직원 간 왕복 | 6 (설정) | `rooms.exchange_count` | 단체방 · 파트방에서 직원의 멘션으로 들어온 턴을 시작하기 직전 (아래 "단체방의 왕복 한도") |

**한도를 바꾸는 길과 사용량 안전망 (V-4 확정).** 위 표의 설정 셋(`studio_max_runs` · `studio_max_exchanges` · `studio_group_max_exchanges`)은 `GET` · `PATCH /studio/machine/limits` 로 읽고 바꾼다 (§6.4). 새 설정을 만들지 않는다 — 바꾼 값은 `studio/limits.py` 가 `relay.config.local.yaml`(이 컴퓨터만, `Config.load` 가 이미 읽는 파일)에 적은 뒤 실행 중인 `Config` 에 넣으므로, 큐와 왕복 검사가 매번 설정을 읽는 대로 재시작 없이 바로 적용되고 재시작 뒤에도 남는다. 쓰기가 실패하면 값을 바꾸지 않는다(500). 범위는 `max_runs` 1–20, `max_exchanges` 1–200, `group_max_exchanges` 1–100 의 정수이고 0 · 음수 · 문자 · 불리언은 거부한다(422). `relay.config.local.yaml` 은 바꾸는 키의 줄만 고쳐 주석과 나머지 줄을 보존한다(줄 끝 `# …` 주석도 남고, 없는 키는 끝에 붙는다). 바꾸는 키가 한 줄짜리 최상위 키가 아니거나 결과가 기대와 다르면 전체를 다시 써서 주석이 사라진다(`repos.save_local_repo` 는 늘 그렇다).

사용 창이 `Config.studio_usage_warn_at`(기본 `0.90`, 0 이하면 끔. `providers.py` 의 `switch_at_utilization` 95% 전환보다 먼저 알린다. 그 규칙과 엔진은 고치지 않았다)에 닿으면 알린다 (§5.9). 새 설정 `Config.studio_usage_hold`(기본 `False`)를 켜면 닿은 동안 큐가 **새 작업 시작(T3, `backlog` 작업)** 을 미룬다 — 이미 `working` 인 작업의 재개(T5 · T8)는 막지 않는다. 미룬 일은 owner 에게 `budget` 결재로 묻는다: 작업도 런도 없는 `budget` 항목이고 `payload.stop_reason = usage_hold`(`scope = run`, `stage = ''`, 금액 둘은 0, 그 밖에 `key` · `provider` · `window` · `utilization` · `resets_at`)다. 새 `kind` 는 만들지 않았다 — 새 `kind` 는 `inbox` 의 CHECK 를 고치는 DB 이전이 필요하고, `backlog` 작업은 차단 결재를 가질 수 없어(F2) `question` 은 쓸 수 없다. 사용 창마다(`key` = provider · window · `resets_at`) 한 번만 묻는다. `approve` 는 그 창이 초기화될 때까지 큐를 풀고, `reject` 는 그 창이 초기화될 때까지 보류를 유지한다(다시 묻지 않는다 — 창의 (provider, window, `resets_at`)이 바뀔 때까지다. 풀려면 창 초기화를 기다리거나 `studio_usage_hold` 를 끈다. 바꿀지는 사람이 정한다). 둘 다 엔진을 부르지 않고 항목만 닫으며(`Inbox._decide_hold`) 결정 라우트가 큐를 다시 본다. `revise` 는 없다. 사용 창이 초기화돼 임계 아래가 되면 감시자가 1분마다 줄을 다시 보아 보류가 풀린다. `studio/usage.py` 의 `UsageWatch.held()` 가 큐의 `hold` 이고 `ask()` 가 큐의 `on_held` 다(`TaskQueue` 는 보류한 작업이 있었을 때만 락 밖에서 부른다). `run_id` 가 없는 `budget` 항목은 `Inbox.sweep_runs` 가 닫지 않는다.

왕복 한도의 '멈춤 → 결재 → 재개'는 이미 있고(작업 방: `question` · `exchange_limit` → T4 → `answer` · `continue` → T5 가 `exchange_count` 를 0 으로) `tests/test_studio_usage.py` 가 REST 로 바꾼 한도에서 그 흐름을 못박는다.

**슬롯.** 작업 런 하나가 슬롯 하나를 쥔다. 쥐는 구간은 큐가 슬롯을 준 순간부터 그 런을 진행시킨 스레드가 `engine.advance` 에서 돌아올 때까지다. 작업 상태로 세지 않는다 — T4(`ask_owner`) · T6 직후에는 작업이 이미 `blocked` · `review` 인데 런은 아직 돌고 있다. 이 문서에서 "런이 멈췄다"는 그 작업 런이 슬롯을 쥐고 있지 않다는 뜻이다. 슬롯 장부는 서버 메모리에 두고(`queue.py`), 재시작하면 빈 채로 시작한다(§4.8).

**줄.** `queued_at` 이 있는 작업을 `queued_at` 오름차순(같으면 `id`)으로 보고, 두 한도에 자리가 있는 첫 작업에 슬롯을 준다. 자기 런이 아직 멈추지 않은 작업(T5 · T8 을 런이 도는 중에 받은 작업)은 건너뛴다. 큐를 다시 보는 때는 슬롯이 비었을 때, `queued_at` 이 새로 생겼을 때, 서버가 시작했을 때다.

- `backlog` 에서 줄 선 작업은 슬롯을 얻는 순간 T3 가 일어난다. `working` 에서 줄 선 작업(T5 · T8 뒤)은 상태는 그대로이고 런만 이어진다. 줄 선 `working` 작업에 T4 가 오면 줄에서 빠진다(`queued_at = NULL`). owner 의 `stop` 도 줄에서 빼고 T4 로 간다 (§6.4). 앞서 받은 결정의 `delivery = 'pending'` 은 그대로 남는다(§4.7).
- `blocked` 와 `review` 는 런이 멈추면 슬롯이 빈다. 그래서 한 작업이 결재를 기다리는 동안 같은 프로젝트의 다른 작업이 돌 수 있다 (S-14: 결재는 그 작업만 멈춘다). 같은 폴더에 두 작업의 변경이 섞일 수 있고, T9 의 조건 2가 이 때문에 있다 (T11 도 같은 조건을 쓴다).
- 채팅 턴과 읽기 전용 턴(`workspace: none`)은 큐를 타지 않고 두 한도에 세지 않는다 (S-8 문장의 해석. S-8 이 확정했다 — 이 턴들은 `studio/chat.py` 가 직접 진행시키고, 큐의 슬롯 장부에는 작업 런만 오른다). `[§12]`
- 한 직원이 작업 런 여러 개를 동시에 갖는 것은 막지 않는다. 한도는 위 표의 것뿐이다.
- 프로젝트 쓰기 1은 엔진의 폴더 락과 같은 제약이다. 락은 `inplace` 워크스페이스에만 걸리고, 겹치면 엔진이 그 런을 `failed` 로 끝낸다(`pipeline.py` 1630–1637). 큐가 먼저 줄을 세우므로 정상 흐름에서는 일어나지 않는다. studio 밖의 런(CLI · 대시보드)이 폴더를 쥐고 있을 수 있으므로 슬롯을 주기 전에 `RelayEngine.folder_holder(workdir)`(813–828)를 보고, 있으면 기다린다.

**왕복 한도.** 작업방에서 직원이 `room_post(mentions = [다른 직원])` 으로 그 직원의 턴을 넣을 때마다 `exchange_count` 를 1 올린다. owner 의 글은 세지 않는다. 올리기 전에 값이 한도 이상이면 턴을 넣지 않고 system 이 T4(`question`, `limit` 이벤트)를 한다. T5(왕복 한도로 생긴 결재일 때, §4.3)와 T9 에서 0으로 돌린다. T4 는 작업이 `working` 일 때만이다. `blocked` · `review` 는 §4.5 의 표를 따르고, 그 밖의 상태에서도 턴을 넣지 않고 `limit` 행만 남긴다 (S-9 확정 — `studio/tools.py` 의 `room_post`).

- 세는 것은 실제로 들어간 턴이다 (S-9 확정). 글 하나가 두 명을 멘션해 둘 다 들어가면 2 를 올리고, 넣지 못한 턴(§9.5)은 세지 않는다. 담당을 멘션하면 그 글이 작업 런에 끼어든 것이 턴이고, 다른 구성원을 멘션하면 읽기 전용 턴이다 (§8.4). 올릴 때 `task.updated`(`change = exchange`, §7.4)를 낸다.
- 한도(`Config.studio_max_exchanges`)는 글 하나에 한 번, 글을 만들기 전 모든 검사 뒤에 본다. 닿으면 글도 만들지 않는다 — 거부될 글이 T4 를 일으키지 않게 순서를 맨 뒤로 뒀다.
- T4 를 한 그 호출 뒤에는 `ask_owner` 와 같은 뒤처리(§8.3)를 한다: 부른 턴이 작업 런이면 대기 타이머, 읽기 턴이면 바로 끊는다 (§5.7). 이미 결재를 기다리는 작업에서 다시 한도에 닿은 호출은 `limit` 행만 남기고 뒤처리를 부르지 않는다 — 부르면 타이머가 새로 걸려, 턴을 끝내지 않는 런이 끊기지 않는다.
- 알려진 틈: 검사와 올리기 사이를 잠그지 않는다. 동시에 온 멘션이나 한 글의 여러 멘션이 한도를 조금 넘길 수 있다.

**단체방의 왕복 한도.** 단체방(§3.6)은 — 파트방도 같다 — 작업이 없어 `rooms.exchange_count` 로 방 단위로 센다. owner 의 글 하나에서 시작된 직원 간 왕복이 세는 대상이다 (`studio/chat.py` 의 `ChatTurns._group_turn`).

- 세는 것: 직원의 멘션(`room_post(mentions=[…])`, §8.4)으로 들어온 턴이 글을 남기고 끝난 것. 글 없이 넘어간 턴(§9.5), `done` 으로 끝나지 못한 턴, owner 의 글로 들어온 턴은 세지 않는다. owner 의 글이 넣어 둔 차례를 기다리는 직원을 다른 직원이 멘션해도 그 턴은 owner 의 글로 들어온 턴이다. 올릴 때 `room.updated` 를 낸다 (§7.4).
- 한도(`Config.studio_group_max_exchanges`, 기본 6)는 멘션으로 들어온 턴을 시작하기 직전 한 곳에서 본다. 값이 한도 이상이면 그 턴을 넣지 않는다. 멘션한 글은 방에 그대로 남는다 — 작업방과 달리 글을 거부하지 않고, 결재도 `limit` 행도 만들지 않는다. 방에 시스템 글 하나(`meta.event = group_exchange_limit`, `meta.limit`, §7.6)를 쓰고 그 글이 owner 에게 즉시 푸시된다 (§5.9). 이미 멈춘 방에서는 시스템 글을 다시 쓰지 않는다.
- 멈춘 방인지는 그 방의 글로 본다: 새 글부터 거슬러 보아 `group_exchange_limit` 글이 owner 의 글과 `group_continued` 글보다 먼저 나오면 멈춘 방이다. 글 수에 한도를 두지 않고 owner 의 글이나 한도 글을 만날 때까지, 없으면 방의 처음까지 쪽(`MAX_PAGE`)을 넘겨 거슬러 본다. 칸을 따로 두지 않아 서버를 다시 띄워도 같고, 시스템 글이 한 번만 남는다.
- 다시 세는 때는 둘이다. owner 가 그 방에 글을 쓰면 0 으로 돌린다 — 새 사슬이고, 턴은 그 글의 대상(멘션한 직원, 없으면 빠른 판단에서 '예' 인 직원)에게만 넣는다 (§6.6). `POST /studio/rooms/{id}/continue`(이어 가기)는 멈춘 방에서 0 으로 돌리고 세워 둔 턴을 넣는다 — 자기가 읽은 곳(`room_members.fed_message_id`) 뒤에 자기를 멘션한 직원 글이 있는 구성원의 턴이다 (§6.4). 읽은 곳 뒤의 글을 끝까지 쪽을 넘겨 본다 — 나중에 초대되어 턴을 받은 적 없는 직원은 방의 처음부터 읽는다.
- 방의 턴은 한 번에 하나라 (§6.6) 시작 직전의 값이 정확하다: 한 글이 여럿을 멘션해도 한도를 넘지 않는다. 설정이 `null` 이면 세기만 하고 보지 않는다.
- 알려진 틈: owner 가 일부 직원만 멘션해 글을 쓰면, 그 전에 세워 둔 다른 직원의 멘션 턴은 이어지지 않는다 (멈춘 방이 아니게 되어 이어 가기도 아무것도 하지 않는다). 그 직원은 다음에 자기 턴이 올 때 그 글을 읽는다. 턴이 도는 중에 owner 가 글을 쓰면 0 으로 돌린 뒤 그 턴이 1 을 올릴 수 있다.
- 1:1 방에서 직원이 한 멘션은 턴을 넣지 않고 세지도 않는다. 왕복 한도는 작업방 · 단체방 · 파트방에만 있어 그 방에는 안전망이 없기 때문이다. 파트방은 단체방과 같은 길을 타서 위 규칙이 그대로 간다.

**선행 조건.** 작업의 선행이 "채워졌다"의 뜻은 캠페인의 진행 방식에 따른다 (CAMPAIGN.md 467–471행).

| 방식 | 채워진 것으로 보는 선행의 상태 |
|---|---|
| `nonstop` | `review` 또는 `done` |
| `milestone` | `is_milestone = 1` 인 선행은 `done`, 나머지는 `review` 또는 `done` |
| `each` | `done` |
| 캠페인 없는 작업 | `each` 와 같다 (S-14 확정) `[§12]` |

`rejected` · `cancelled` 인 선행은 채워지지 않는다. 그 뒤 작업은 시작되지 않고 owner 에게 알린다(선행을 빼거나 바꾼다).

### 4.7 런 상태와 작업 상태의 대응

**런이 멈췄을 때.** `engine.advance(run_id, resume)` 는 런이 끝나거나 멈출 때까지 돌고 그때의 `RunState` 를 돌려준다(`pipeline.py` 1488–1509). 작업 런을 진행시킨 스레드는 여기서 돌아오면 아래 네 걸음을 순서대로 한다.

1. 슬롯을 놓는다.
2. 미전달 결정문을 넣는다. `run_id` 가 이 런이고 `delivery = 'pending'` 인 결재를 `closed_at` 순으로 하나씩 `engine.interject(run_id, 결정문)` 하고 `delivered` 로 바꾼다(§3.13). 그때 작업이 `blocked` · `review` 여도 한다. 런이 `done` 이면 `interject` 가 거부하므로(`pipeline.py` 682–683) 넣지 않고 `pending` 으로 둔다.
3. 런 상태와 그때의 작업 상태를 보고 아래 표대로 처리한다.
4. 큐를 다시 본다.

표의 `working` 은 줄 서지 않은(`queued_at` 이 빈) 작업이다. 줄 서 있는 경우는 마지막 행이다. 표의 "걸음 2"는 위의 2다.

| 런 상태 | 그때 작업 상태 | 처리 |
|---|---|---|
| `awaiting_approval` (예산) | `working` | system 이 T4 — `budget` |
| `awaiting_approval` (설계 게이트) | `working` | system 이 T4 — `design_approval` |
| `awaiting_approval` | `blocked` | 이미 `ask_owner` 나 가드가 T4 를 했다. 더 하지 않는다 (미전달 결정문은 걸음 2에서 넣었다) |
| `done` | `review` | 이미 T6 가 됐다. 더 하지 않는다 |
| `done` | `working` | system 이 T4 — `question` ("검수 요청 없이 끝났다") |
| `done` | `blocked` | 더 하지 않는다. 걸음 2를 못 한다 — 미전달 결정문은 `pending` 으로 남는다. 이 런은 이을 수 없다 (아래 표의 `done` 행) |
| `failed` · `cancelled` | `working` | system 이 T4 — `question` (오류 내용) |
| `failed` · `cancelled` | `blocked` | 서버가 끊은 것이다. 더 하지 않는다 (미전달 결정문은 걸음 2에서 넣었다) |
| `awaiting_approval` · `failed` · `cancelled` | `review` | 더 하지 않는다. 런이 어떻게 끝났는지는 검수 패키지에 싣는다 (§5). 통과 · 되돌리기(T7 · T9)는 이 런을 먼저 `cancel` 한다 (§4.3) |
| 모든 상태 | `working`, 줄 서 있음 | 런이 멈추기 전에 owner 가 이미 결정했다(T5 · T8). T4 를 하지 않는다. 결정문은 걸음 2에서 들어갔다(런이 `done` 이면 `pending` 으로 남는다). 줄에 그대로 둔다 (아래 "런을 이을 때"). `stop` 은 줄에서 먼저 빼므로 이 행에 오지 않는다 (§6.4) |

예산과 설계 게이트의 구분은 런의 정지 기록으로 한다 — 예산은 `_write_stop(run, "budget", …)`(`pipeline.py` 1588) 또는 `pending_budget_stage`(202), 설계 게이트는 `_write_stop(run, "awaiting_approval", …)`(1787). 정지 기록은 `run.baton.stop` 의 `StopNote`(`baton.py` 38–48 · 86)이고 `kind` 가 `budget` · `stage_budget` · `awaiting_approval` · `failed` · `cancelled` 중 하나다. `stage_budget` 은 단계 예산에서 멈춘 것이고(`pipeline.py` 1730) 결재 종류는 `budget` 과 같다 (§5.8 가). 런이 `done` 이면 비어 있다(`pipeline.py` 1795).

에디터 무응답(M-7)은 표에 행을 따로 두지 않는다. 엔진의 `_check_mcp_ready` 가 잡으면 런이 `failed` 로 끝나 `failed` · `working` 행이 T4 를 한다. `blocked` · `review` 에서 서버가 끊은 런(§4.5)은 `cancelled` · `blocked` 행과 `review` 행을 따른다. T3 직후의 검사에 걸려 진행하지 않은 런은 `pending` 으로 남고 진행 스레드가 없어 이 표를 거치지 않는다 — T4 는 검사한 쪽(`work.py` 의 `WorkEffects.editor_ready`)이 하고, T5 뒤에는 아래 표의 `pending` 행대로 잇는다. `working` 에서 런 도중에 감지하면 서버가 T4 를 먼저 하고 끊으므로(§4.5) 진행 스레드가 돌아왔을 때 작업은 이미 `blocked` 다 — `cancelled` · `blocked` 행을 따르고 결재는 하나다.

`failed` 가운데 둘은 T4 로 보내지 않고 기존 `CampaignRunner._drive_run`(`campaign.py` 431–465)처럼 스스로 다시 잇는다 (S-8 확정): 오류가 "서버 재시작"으로 시작하면 바로, "다른 실행(" 이 들어 있으면(폴더를 다른 런이 쓰는 중) 기다렸다가 — 기다림은 큐가 슬롯을 주기 전의 `folder_holder` 확인이다 (§4.6). 다시 이을 때는 작업을 줄에 세운다(`queued_at`. system 의 `queued` 이벤트이고 `detail_json` 은 `{resume: true}` 다). 사용량 한도로 `failed` 된 런은 아직 스스로 잇지 않고 T4(`run_failed`)로 보낸다 — 한도가 풀릴 때까지 줄에서 기다리게 할 장치가 없어, 바로 세우면 같은 실패를 되풀이한다 (S-14 확정 — 그 장치를 두지 않았다. `work.resumes` 가 스스로 잇는 것은 위의 둘뿐이다). 진행 스레드가 `advance` 에서 돌아온 것이 아니라 예외로 돌아왔으면(런을 진행시키지 못했다) 런은 그대로이므로 스스로 잇지 않는다 — 다시 세우면 같은 예외로 끝없이 돈다. 그때는 그 예외 문구를 `error` 로 T4(`run_failed`)를 올린다. 런이 `pending` 인 채 진행시키지 못한 경우도 같다 — 두면 작업이 `working` · 줄 밖으로 남아 아무도 잇지 않는다 (S-8 확정). 예외로 돌아왔어도 그 진행 중에 엔진이 이 런의 실패를 기록했으면(진행 직전보다 뒤에 쓴 `run_failed` 이벤트) 런은 실제로 `failed` 로 끝난 것이다 — `error` 와 본문에 예외 문구가 아니라 `RunState.error` 를 쓰고, 이때도 줄에는 다시 세우지 않는다. 예외로 돌아왔는데 런이 `running` 이면 다른 곳(대시보드 · CLI · 다른 스레드)이 그 런을 진행 중인 것이다("already advancing"). T4 를 하지 않고 줄에도 세우지 않는다 — 줄에 세우면 큐가 런이 어떻게 끝났든 이어 버린다. 큐가 그 작업을 메모리에 기억해 두고 줄을 볼 때마다, 그리고 `retry_after` 타이머로 다시 보다가, 런이 멈추면 위 표를 예외 없이 적용한다. 이 기억은 재시작하면 사라지고 그때는 §4.8 의 걸음 1이 같은 일을 한다 (S-8 확정).

**런을 이을 때 (T5 · T8).** 두 걸음이다. 결정문 넣기(`interject`)는 런이 멈춘 것을 확인한 즉시 하고 슬롯을 기다리지 않는다 — 런이 이미 멈춰 있으면 결정한 직후, 아직 돌고 있으면 진행 스레드가 돌아올 때(위의 걸음 2)다. 진행(`approve` · `advance`)은 슬롯을 얻은 뒤에 한다. 넣었는지는 `inbox.delivery` 가 기억한다(§3.13). 그래서 런이 도는 중에 T5 를 받았고 그 런이 `ask_owner` 를 또 불러 T4 가 다시 와도, 첫 결정문은 `pending` 으로 남아 있다가 스레드가 돌아올 때 들어간다. `interject` 는 됐는데 `delivered` 로 바꾸기 전에 서버가 죽으면 복구 때 다시 넣는다(§4.8). 결정 직후의 넣기와 걸음 2가 겹쳐도 마찬가지다 — 적어도 한 번 들어가고, 같은 결정문이 두 번 들어갈 수 있다. MCP 를 쓰는 작업이면 슬롯을 얻은 뒤 진행(`approve` · `advance`) 직전에 에디터 검사를 한다(§4.3 T3). 무응답이면 진행하지 않고 슬롯을 놓은 뒤 system 이 T4(`question`)를 한다 — 런은 멈춰 있던 상태 그대로 남는다. `editor_unresponsive` 를 `retry` 로 결정한 뒤에도 같은 길이다 — 줄, 슬롯, 검사, 진행 순이고 검사에 또 걸리면 결재가 다시 생긴다.

| 런 상태 | 잇는 방법 |
|---|---|
| `running` (T4 · T6 직후, 아직 슬롯을 쥐고 있다) | 결정문을 넣지 않고 기다린다(`delivery` 는 `pending`). 도는 런에 `interject` 하면 끝내는 중인 대화에 바로 들어간다(`pipeline.py` 684–686). 결정이 먼저 왔으므로 서버는 그 런을 끊지 않는다 — T4 의 대기 타이머는 발화할 때 작업이 아직 `blocked` 이고 그 결재가 아직 `pending` 일 때만 끊는다 (§8.3). 그 확인과 `cancel` 사이에 결정이 들어온 경우는 §8.3 의 알려진 틈 (나) · (다)다. `notes.jsonl` 에는 live 여부와 무관하게 적혀(687–688) 다음 단계가 시작할 때 또 합쳐지는데(`_merge_notes`, 700–706), 마지막 단계에서 받으면 합쳐지지 않은 채 런이 `done` 이 된다. 진행 스레드가 돌아오면 걸음 2에서 넣고, 그때의 런 상태에 맞는 아래 행을 따른다 |
| `awaiting_approval` | `engine.interject(run_id, 결정문)` → (슬롯) → `engine.approve(run_id)` → `engine.advance(run_id)` |
| `failed` · `cancelled` · `pending` | `engine.interject(run_id, 결정문)` → (슬롯) → `engine.advance(run_id, resume=True)` |
| `done` | 같은 런을 다시 연다 (S-12 확정). `RESUMABLE`(`pipeline.py` 228)에 `done` 이 없고 `interject` 도 거부하므로(682–683), 결정문을 넣는 자리(`studio/runs.py` 의 `EngineHooks.interject`)가 먼저 `RelayEngine.reopen(run_id)` 을 부른다 → `engine.interject(run_id, 결정문)` → (슬롯) → `engine.advance(run_id, resume=True)`. `reopen` 은 런을 `pending` 으로 돌리고 돌아갈 단계를 정한다 — 릴레이의 뒤에서부터 찾아 처음 나오는 `on_retry` 가 가리키는 단계, 없으면 마지막 단계. 그 단계의 `stage_sessions` 에 `RunState.done_sessions`(200) 값을 넣어 끝낸 대화를 그대로 잇는다(런 이벤트 `run_reopened` · `stage_resumed`). 새 런을 만들지 않으므로 `tasks.run_id` · 결재의 `run_id` · 비용 · 반영 전의 변경과 스냅샷 · 쓰지 않은 1회 허용(§5.5)이 그대로다. 변경이 `applied` · `discarded` · `rolled_back` 인 런과 아직 스레드가 쥔 런은 다시 열지 않는다(`ValueError`) — 그때 결정문은 `delivery = 'pending'` 으로 남는다. 재작업(T8), `no_review_request` 의 답(T5), 진행 스레드의 걸음 2, 재시작 복구가 모두 이 한 자리를 지난다. 다시 열린 단계는 끝낸 대화를 잇는 것이라 `retry_model` · `retry_effort` 로 올리지 않는다 — `advance` 는 이어받은 대화가 `done_sessions` 의 값과 같으면 승급하지 않는다(`pipeline.py` 1724–1726). 그 뒤 단계들은 엔진의 다시 하기 그대로다 (새 대화, `retry_*` 가 있으면 승급). 비용: 런이 도는 동안 결정된 결재의 결정문은 그 런이 받지 못하고 `pending` 으로 남았다가, 런이 `done` 으로 끝난 뒤 이 길로 런을 다시 열어 돌아갈 단계부터 끝까지 한 번 더 돌린다 — 결정문이 직원에게 닿으려면 피할 수 없는 재실행이다 |

- `approve` 는 런을 `pending` 으로만 돌린다. 진행 스레드는 호출한 쪽이 띄운다 (`pipeline.py` 656–671, `server.py` 817–818).
- `interject` 는 런이 돌든 멈췄든 항상 `notes.jsonl` 에 적는다(`pipeline.py` 687–688). live 로 전달했는지는 그 줄의 `live` 표시로만 남는다. `_merge_notes`(694–707)는 `notes_seen` 뒤의 줄을 `live` 표시와 무관하게 baton 의 `user_notes` 에 합친다. 합치는 때는 다음 단계가 시작할 때다(1593).
- 작업 런은 승인 방식 `ai` 로 만든다 (S-8 확정). `auto` 는 설계 게이트에서 멈추지 않고 "모두 승인됐다"고 알리므로(`pipeline.py` 207–210) 전제 3과 맞지 않는다. `ai` 에서도 엔진이 멈추는 것은 `gate: human` 단계가 `needs_approval` 을 냈고 뒤 단계가 남았을 때뿐이라(1777–1778), 런을 끝내는 기본 수단은 T4 의 `RelayEngine.cancel` 이다.
- 건너뛴 단계는 작업 기록에 남긴다. 엔진은 `optional` 단계를 돌릴 AI 가 없으면(`RunnerError` 의 `unavailable`) 이벤트 `stage_skipped`(`reason` 은 오류 문구 200자)를 남기고 다음 단계로 넘어간다 (`pipeline.py` 1733–1737. 이벤트는 `_event` 374–375 가 `history.add_event` 로 저장한다). 런이 실패하지 않으므로 위의 표로는 드러나지 않는다. 진행 스레드가 `advance` 에서 돌아오면 걸음 3 앞에서 그 런의 이벤트를 읽어(`HistoryStore.events(run_id, after_id)`, `history.py` 232–236 — `id` 순이고 행마다 `id` · `stage` · `kind` · `detail`) `stage_skipped` 하나마다 `task_events` 의 `run` 행을 쓴다. `reason` 은 엔진이 적은 이유, `detail_json` 은 `{run_id, stage, event, event_id}` 이고 `event` 는 `stage_skipped`, `event_id` 는 그 엔진 이벤트의 `id` 다. 런이 어떤 상태로 돌아왔든 쓰고, 방에는 알리지 않는다 — owner 는 검수 패키지의 `incidents[]` 에서 본다 (§5.4). 같은 이벤트를 두 번 쓰지 않게 가리는 키는 `event_id` 다 — 그 작업에 같은 `run_id` · `event_id` 의 `run` 행이 있으면 다시 쓰지 않는다 (S-8 확정). 걸음의 번호는 늘리지 않는다.
- studio 밖(대시보드 · CLI)에서 작업 런을 승인 · 재개 · 취소했을 때 결재와 작업 상태를 맞추는 규칙은 §5.8 (다)가 정했다 (기존 경로 쪽 접점은 §11.3).
- 작업 취소(T10 · T11)는 런을 잇지 않는다. T11 은 멈춘 런을 T9 처럼 정리하고 변경을 되돌린 뒤 `run_id` 를 비운다 (§4.3 T11). 거꾸로 런이 `cancelled` 로 끝났다고 작업이 `cancelled` 가 되지는 않는다 — 위 표대로 T4 이거나 그대로다.

### 4.8 재시작 복구

서버 시작 때 기존 복구(`server.py` 49–53: `engine.recover_interrupted()` → `campaigns.recover()`) 뒤에 studio 복구를 돈다 (S-8. 호출 자리는 확정했다: `server.py` 에 마지막으로 등록한 startup 함수 `start_task_queue` 가 `studio/work.py` 의 `recover(engine, inbox_factory, kick)` 를 부른다 — 기존 복구, 승인 대기 채우기, 시드 직원 뒤라 저장소가 열려 있다. 저장소가 열려 있지 않으면 걸음 3만 한다. 걸음 하나 · 작업 하나가 실패해도 기록만 남기고 나머지를 하며, 예외를 올리지 않는다). 작업 없는 런의 승인 대기를 결재로 채우는 것만은 S-5 가 이미 한다 (§5.8 나의 "시작할 때"). 작업 런의 같은 경우는 아래 걸음 1이 맡는다.

1. 슬롯 장부는 비어 있다. `recover_interrupted`(`pipeline.py` 1016–1029)가 서버가 멈출 때 `running` 이던 런을 `failed`(오류가 "서버 재시작"으로 시작)로 바꿔 둔다. `run_id` 가 있는 `working` · `blocked` · `review` 작업마다 런을 읽는다. `working` 이고 줄 서지 않은 작업의 런이 그렇게 끊겼거나 `pending` 이면(만들고 진행시키기 전에 멈췄다) 줄에 세워(`queued_at`) 다시 잇는다. §4.7 의 스스로 다시 잇는 다른 `failed`("다른 실행(")도 같다. 줄에 세우는 것은 걸음 2 뒤에 한다 — 줄에 서면 큐가 곧 진행시키고, 도는 런에는 결정문을 넣지 않기 때문이다. 그 밖의 상태면 §4.7 의 "런이 멈췄을 때" 표대로 처리한다 (건너뛴 단계의 기록도 함께). 그 전에 `blocked` 인데 런이 `running` 인 작업은 그 런을 `RelayEngine.cancel` 로 끊는다 — T4 응답 뒤의 대기 타이머(§8.3)는 메모리에만 있어 서버와 함께 사라졌다. 남은 시간을 계산하지 않고 바로 끊는다. 이 서버가 돌리던 런은 `recover_interrupted` 가 먼저 `failed` 로 닫으므로 여기에 걸리는 것은 다른 살아 있는 프로세스가 쥔 런뿐이고, 그때 `cancel` 은 디스크에 `cancelled` 를 적을 뿐 그 프로세스를 멈추지 못한다 (엔진의 기존 한계). `review` 작업의 `running` 런은 끊지 않는다 (§4.3 T6). 끊은 작업의 id 는 `recover` 반환값의 `cut` 에 담긴다.
2. `delivery = 'pending'` 인 결재를 모두 찾아(`inbox_undelivered`) 그 `run_id` 의 런에 §4.7 의 걸음 2를 한다. 재시작 직후에는 이 서버가 돌리던 런이 모두 멈춰 있다(1의 `recover_interrupted`). 다른 프로세스가 쥐고 있어 `running` 인 런과 `done` 인 런은 건너뛰고 `pending` 으로 둔다. `interject` 는 했는데 `delivered` 로 바꾸기 전에 서버가 죽었던 결재는 여기서 한 번 더 들어간다.
3. `queued_at` 이 있는 작업은 그대로 줄에 남아 있다. 큐를 한 번 돌린다.
4. 모든 재직 직원의 상태를 §4.9 로 다시 계산한다.
5. `proposed` · `blocked` · `review` 인데 `pending` 결재가 없는 작업이 있으면 고치지 않고 시스템 방에 알린다 (불변식이 깨진 것이다). 서버를 띄울 때마다 같은 작업을 다시 알리지는 않는다 — 시스템 방에 그 작업의 `invariant_broken` 글이 이미 있고 그 뒤로 작업이 바뀌지 않았으면(`tasks.updated_at` 이 그 글의 `created_at` 보다 늦지 않다) 넘어간다. 고쳤다가 다시 깨진 작업은 다시 알린다 (S-8 확정).

**서버를 죽였다 띄운 뒤 (V-3 확정).** 위 걸음은 이미 있고, 엔진과 저장소를 같은 폴더로 새로 열어 아래가 이어지는지를 `tests/test_studio_recovery.py` 가 못박는다. 새로 만든 복구 코드는 없다 — 빈틈이 없었다.
- 끊긴 채팅 · 읽기 턴: 그 턴의 런은 엔진의 `recover_interrupted` 가 `failed` 로 닫는다 (걸음 1은 작업 런만 본다). 답이 없던 글은 `room_members.fed_message_id` 가 앞서지 않아 그대로 남고, 새 `ChatTurns` 가 다음 `request` 에서 한 번만 읽는다 — 다시 `request` 해도 새 글이 없으면 런을 만들지 않는다.
- 끊긴 작업 런: 새 엔진이 `failed` 로 닫은 뒤 걸음 1이 줄에 다시 세우고(`queued`, `resume: true`), 새 `TaskQueue` 가 집어 끝까지 간다.
- 기다리던 결재: `pending` 으로 남고 `reminded_at` 도 DB 에 있다. 새 `Pusher` 는 그 시각에서 `studio_remind_after` 가 지나기 전에는 보내지 않고 지나면 보낸다 (§5.9).
- 단체방: 줄(`ChatTurns._rooms`)은 메모리뿐이라 비고, `exchange_count` 와 멈춘 방 판정(`api.group_halted`) · 밀린 차례(`api.group_owed`)는 DB 의 방과 글에서 읽힌다. 멈춘 방에서 멘션해도 두 번째 알림은 쓰이지 않고, owner 가 글을 쓰면 다시 세어 구성원이 차례대로 한 번씩 턴을 받는다.
- 직원 상태: 걸음 4가 §4.9 로 다시 계산해, 서버가 죽으며 어긋난 `employees.status` 를 바로잡는다.

**절전 방지 (V-3 확정, `studio/awake.py`).** 서버는 며칠씩 떠 있으므로 일하는 동안 PC 가 잠들지 않게 한다. 작업 런이 슬롯을 쥔 동안(`TaskQueue._work`: 시작에서 세고 슬롯을 놓을 때 푼다)과 채팅 · 읽기 턴이 도는 동안(`ChatTurns._work` · `_group_work`)을 세고(`Awake.hold`), 0 에서 1 이 되면 켜고 1 에서 0 이 되면 끈다 — 도는 것이 없으면 시스템이 다시 잠들 수 있다. 시스템 호출은 주입하는 `setter(on)` 이고 기본은 Windows 의 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` 다. 그 함수는 부른 스레드에만 걸리고 켜는 스레드와 끄는 스레드가 다르므로 전용 스레드 하나가 모든 호출을 한다. Windows 가 아니거나 호출이 실패하면 로그만 남기고 런은 그대로 간다. `server.py` 는 startup 의 `KEEPER.enable()` 과 shutdown 의 `disable()` 만 연결한다 — `enable` 전에는 아무것도 부르지 않아 테스트가 실제 절전 API 를 건드리지 않는다. 전원 설정 · 시작 프로그램 등록 · `supervise.py` 는 고치지 않는다.

### 4.9 직원 상태 유도

`employees.status` 는 아래 규칙의 첫 번째로 맞는 값이다. 작업의 전이와 같은 트랜잭션에서 갱신하고, 바뀌면 `employee.status` 를 낸다.

1. owner 가 휴직으로 뒀다 → `on_leave`
2. 담당인 작업 중 `working` 이 하나라도 있다 → `working`
3. 담당인 작업 중 `blocked` 가 하나라도 있다 → `blocked`
4. 그 밖 → `idle`

- 채팅 턴은 상태를 바꾸지 않는다. "지금 답하는 중"은 `run.live` 로 본다.
- 진행 중인 작업이 있는 직원의 배치 변경 · 휴직 켜기 · 퇴사 (H-3 확정). 그 직원이 담당인 작업 가운데 `working` · `blocked` · `review` 가 있거나 줄에 선(`queued_at`) `backlog` 가 있으면 세 경로 모두 `precondition_failed`(`failed=["active_tasks"]`)로 거절한다 — 런이 도는 중에 프로젝트나 재직 상태가 바뀌면 T3 의 검사와 어긋나고, `review` 는 반려(T8)로 다시 `working` 이 되기 때문이다. owner 가 먼저 작업을 멈추거나 담당을 바꾼다. 줄에 서지 않은 `backlog` 작업은 담당을 그대로 둔다(퇴사 · 이동 뒤에는 시작 요청이 거절된다, §4.10 알려진 틈). 휴직을 끌 때는 상태를 §4.9 의 규칙으로 담당 작업에서 다시 계산한다(`employees.derive_status`). 휴직은 작업 시작(T3)과 `task_propose` 만 막는다 — 채팅 턴과 단체방 초대는 막지 않는다 (§3.6).

### 4.10 캠페인 상태

| 전이 | 누가 | 뜻 |
|---|---|---|
| (없음) → `draft` | owner | 작업을 모으는 중. 자동 시작하지 않는다 |
| `draft` → `running` | owner | 선행이 채워진 작업을 자동으로 시작한다 (담당이 정해진 것만) |
| `running` ↔ `paused` | owner | 멈추면 새 작업을 시작하지 않는다. 진행 중인 작업은 건드리지 않는다 |
| `running` → `done` | system | 모든 작업이 `done` · `rejected` · `cancelled` 중 하나다 |
| `draft` · `running` · `paused` → `cancelled` | owner | 새 작업을 시작하지 않는다. 캠페인 취소는 작업을 취소하지 않는다 — 진행 중인 작업은 그대로 가고, 그만둘 작업은 하나씩 취소한다 (T10 · T11) |

담당이 없어 시작하지 못한 작업은 "배치 필요"로 알린다 (S-14). 알림의 형태는 §5.9 가 정했다 — 결재가 아니라 시스템 방의 글과 푸시이고, 글의 `meta` 는 §7.6 이다.

위 표의 상태와 전이는 S-14 가 확정했다 (`tasks.CAMPAIGN_STATES` · `CAMPAIGN_ALLOWED`).

**자동 진행 (S-14 확정 — `studio/campaigns.py` 의 `Autopilot.look`).** 플래너도, 캠페인을 붙들고 도는 루프도 없다. 아래의 계기마다 `running` 캠페인의 작업을 `sort_order` 순으로 한 번 훑어, 넷을 모두 채운 작업에 system 이 시작을 요청한다 (§4.3 T3 의 계기. `queued` 이벤트의 사유는 "캠페인 자동 진행"). 그 뒤는 owner 가 시작을 누른 작업과 같다 — 줄을 서고, 큐가 슬롯을 주면 T3 가 일어난다 (§4.6).

1. `backlog` 이고 줄 서 있지 않다.
2. 담당이 있다. 없으면 건너뛴다 ("배치 필요").
3. 줄 선 적이 한 번도 없다 (`task_events` 에 `queued` 행이 없다). 되돌린 작업(T9), owner 가 줄에서 뺀 작업, 런을 만들지 못해 줄에서 빠진 작업은 스스로 다시 시작하지 않는다 — owner 가 고친 뒤 시작을 누른다. 계기마다 다시 세우면 같은 실패가 되풀이되고 owner 가 고칠 틈이 없다. 예외는 하나다 — 캠페인이 `paused` · `cancelled` 가 되어 system 이 줄에서 뺀 것(아래 "멈춤")은 줄 선 적으로 세지 않는다. `queued` 행 뒤에 그 `unqueued` 행이 오면 그 `queued` 는 없던 것으로 본다 (`campaigns._stood`).
4. 선행이 채워졌다 (§4.6 "선행 조건"). 볼 때마다 캠페인의 `mode` 를 읽으므로 방식을 바꾸면 다음 시작부터 적용된다 (§3.9).

시작 요청이 거절되면(담당이 휴직 중이거나 다른 프로젝트에 배치돼 있다) 기록만 남기고 다음 작업을 본다. 거절은 `queued` 이벤트를 남기지 않아 위 3 에 걸리지 않는다 — 담당이 복귀하거나 배치가 맞은 뒤의 다음 계기에 시작한다.

- **계기.** 캠페인이 `running` 이 됐을 때. 작업이 만들어졌을 때(C2), 제안이 승인 · 거절됐을 때(T1 · T2), 검수를 요청했을 때(T6), 통과 · 취소됐을 때(T7 · T10 · T11), 담당 · 선행이 바뀌었을 때 — 이때는 그 작업의 캠페인만 본다. 직원의 배치 · 휴직 · 퇴사 · 이름이 바뀌었을 때(H-3 — 이름은 훑을 일이 없어도 같은 헬퍼가 부른다)는 `running` 캠페인을 모두 본다. 작업 내용을 고쳤을 때(`edited`. 캠페인이 바뀌었을 수 있다)와 서버가 시작했을 때(§4.8 의 복구 뒤)는 `running` 캠페인을 모두 본다. `progress` · `run` 처럼 잦은 변화에서는 보지 않는다 (`campaigns.LOOKS`).
- **멈추는 곳.** 캠페인이 스스로 서는 일은 없다. 결재(`blocked`)나 검수(`review`)에 선 작업은 뒤 작업의 선행을 채우지 않을 뿐이고(방식에 따라, §4.6), 런이 멈추면 슬롯을 놓으므로 그 작업에 기대지 않는 줄기는 계속 간다.
- **끝.** 볼 때마다, 캠페인에 작업이 하나 이상 있고 모두 `done` · `rejected` · `cancelled` 이면 system 이 `running` → `done` 으로 넘긴다. 작업이 하나도 없는 캠페인은 `running` 으로 둔다 — `done` 에서 돌아오는 전이가 없다.
- **멈춤 (S-14 확정 — `TaskMachine.transition_campaign` 의 `_hold_line`, 그물은 `Autopilot._hold`).** 큐(§4.6)는 캠페인 상태를 보지 않는다. 그래서 캠페인을 `paused` · `cancelled` 로 바꾸는 전이가, 상태를 바꾸는 그 트랜잭션 안에서 자동 진행이 줄 세운 `backlog` 작업(가장 새 `queued` 행이 system 의 "캠페인 자동 진행")을 줄에서 뺀다 — system 의 `unqueued` 이벤트이고 사유는 "캠페인을 일시정지해 줄에서 뺐다" · "캠페인을 취소해 줄에서 뺐다"(`campaigns.HELD`)다. 스트림에는 `campaign.updated` 다음에 뺀 작업마다 `task.updated` 가 나간다. 상태 변경과 줄 빼기가 한 커밋이라, 전이 뒤 훑기 전에 서버가 죽어도 멈춘 캠페인이 세운 작업은 줄에 남지 않는다. 슬롯은 얻었지만 아직 T3 전인 작업도 빠진다 — 큐의 진행 스레드는 줄에서 빠진 것을 보고 런을 만들지 않은 채 슬롯을 놓는다. T3 가 먼저 커밋됐으면 그 작업은 `working` 이라 건드리지 않는다. owner 가 직접 시작을 누른 작업은 줄에 둔다 — 캠페인이 아니라 owner 의 요청이다. 슬롯을 기다리는 `working` 작업(T5 · T8 뒤)과 진행 중인 작업은 건드리지 않는다. 전이 뒤의 훑기(`Autopilot._hold`)가 같은 것을 한 번 더 본다 — 전이와 겹쳐 돌던 훑기가 그 커밋 뒤에 세운 작업을 거두는 그물이다 (아래 알려진 틈). `paused` → `running` 으로 돌아오면 다음 훑기가 뺀 작업을 다시 세운다 (위 3 의 예외).
- **도중 변경 (S-14 확정 — `TaskMachine.edit_campaign`).** `title` · `goal` · `mode` 는 캠페인이 끝나기 전에 바꿀 수 있다 (`campaign.updated`). 바꾼 뒤 바로 훑으므로 새 `mode` 로 선행이 채워진 작업은 그때 시작한다. 이미 줄 섰거나 시작된 작업은 그대로 간다 — 엄격한 방식으로 바꿔도 되돌리지 않는다.
- **알림 (S-14 확정 — §5.9).** 훑다가 담당 없는 `backlog` 작업을 만나면 "배치 필요"를, 위 1–3 을 채웠는데 직접 선행 가운데 `rejected` · `cancelled` 가 있는 작업을 만나면 "선행 거절"을 시스템 방에 쓴다. 뒤의 것은 어느 방식에서도 선행이 채워지지 않으므로 시작을 요청하지 않는다. 각각 한 번만 쓴다 (§5.9).
- **REST (S-14 확정 — §6.4).** 만들기는 `draft` 로(`TaskMachine.create_campaign`. `campaign.updated` 로 알린다 — 생성 전용 이벤트는 없다, §7.4). `start` 는 `draft` 에서만, `resume` 은 `paused` 에서만 받는다 — 둘 다 `running` 으로 가는 전이라 경로가 구분한다. `pause` 는 `running` 에서, `cancel` 은 위 표가 허용하는 곳에서. 그 밖은 `illegal_transition` 이다.
- **확장 지점.** 기존 `CampaignRunner`(`campaign.py`)에는 클래스 끝의 `studio`(주입 함수)와 `advance_studio(campaign_id=None, again=False)`(예외를 올리지 않는 진입)만 더했다. `again` 은 캠페인이 `running` 이 된 직후의 훑기에서만 참이고(`tasks.CAMPAIGN_STARTED`), 그때만 `studio` 에 키워드로 넘긴다 (§5.9 의 다시 알림). `server.py` 가 `Autopilot.look` 을 `studio` 에 넣고, `WorkEffects.campaign`(위의 계기)과 서버 시작이 `advance_studio` 를 부른다. `campaign.py` 는 studio 를 import 하지 않는다. `_run_task` · `_drive` 는 그대로다 — studio 캠페인은 런을 T3 와 큐가 만들고 밀어, 한 폴더에서 차례로 도는 기존 루프와 겹치지 않는다. `runs/campaigns/<cid>.json` 도 만들지 않는다 (§3.9 `runner_id`).

알려진 틈:

- 같은 초에 줄 선 작업은 `id` 순으로 시작한다 (§4.6 "줄"). 한 번의 훑기가 여럿을 세우면 `sort_order` 와 다를 수 있다.
- owner 가 직접 줄 세운 작업은 캠페인을 `paused` · `cancelled` 로 바꿔도 줄에 남아 큐가 시작한다 (의도한 것 — 위 "멈춤"). 그만두려면 그 작업의 시작 요청을 거둔다.
- 훑기가 도는 중에 캠페인이 `paused` · `cancelled` 가 되면, 그 훑기가 전이의 커밋 뒤에 세운 작업은 줄에 남는다 — 시작 요청은 캠페인 상태를 다시 보지 않는다. 전이 뒤의 훑기(`Autopilot._hold`)가 거두지만, 그 훑기가 먼저 끝났거나 그 전에 서버가 죽으면 그 캠페인을 id 로 다시 훑을 때까지 줄에 남아 큐가 시작할 수 있다. 서버 시작 때의 훑기는 `running` 캠페인만 보므로 거두지 않는다.
- "선행 반려" 표시는 직접 기다리는 작업 중 `working` · `blocked` · `review` 만 받는다. 이미 `done` 인 뒤 작업과 간접으로 기다리는 작업은 표시하지 않는다 (잠정). 표시는 그 작업이 세 상태를 벗어날 때(T7 · T9 · T11) 내린다 (§4.3 T8). 불리언 하나라 반려된 선행이 여럿이어도 어느 것 때문인지는 `flag` 이벤트에서만 알 수 있다.
- "배치 필요" · "선행 거절"을 캠페인 시작 · 재개 때 다시 알리는 것은 전이가 그 뒤의 훑기에 넘기는 힌트다 (§5.9). `running` 으로 가는 전이가 커밋된 뒤 그 훑기 전에 서버가 죽으면 다시 알리지 않는다 — 먼저 쓴 글은 방에 남아 있다. 알릴 후보가 있는 훑기마다 시스템 방의 글을 처음부터 읽는다.
- 담당의 휴직 복귀나 프로젝트 배치 변경은 작업의 변화가 아니어서 작업 쪽 계기로는 훑지 않는다 (H-3 확정). 직원의 수명주기 라우트(`assign` · `leave` · `retire` · `PATCH`)가 변경을 커밋한 뒤 `api._look_at_campaigns` 로 `advance_studio()` 를 부르고 `running` 캠페인을 모두 훑는다. 값이 그대로면 훑지 않는다. 훑기가 실패해도 응답은 그대로다(로그만).
- 퇴사했거나 다른 프로젝트로 옮긴 직원이 담당인 채 줄에 서지 않은 `backlog` 작업은 시작 요청이 계속 거절되고 "배치 필요" 알림도 나오지 않는다 (알림은 담당이 없는 작업만 본다). owner 가 담당을 바꾼다. 퇴사 · 이동 때 담당을 비우지 않는다 (H-3 확정).
- 시스템 방 · 작업방에 글로 쓰는 알림(배치 필요 · 선행 거절 · 선행 반려)은 즉시 등급 푸시로도 간다 (N-2 확정, §5.9).

---

## 5. 결재

근거: CAMPAIGN.md 76행(결재 9가지), S-5 · S-9 · S-10 · S-12 · S-14, H-2 · H-6, M-7, N-1–N-3, A-9 · A-10. 종류 식별자는 §1.5, 테이블은 §3.13, 작업 전이와의 연결은 §4.3 을 따른다.

### 5.1 공통 규칙

- 결재는 `inbox` 행 하나다. 행을 만드는 것은 서버뿐이다. 직원은 studio MCP 도구(§8)를 부르고 서버가 그 안에서 만든다. 가드 훅도 행을 직접 만들지 않는다 — `POST /studio/guard/check`(§6.4)를 부르고 서버가 그 안에서 `risky_command` 결재나 `guard` 기록을 만든다.
- 강제 결재(서버가 스스로, 또는 가드 훅의 요청을 받아 만든다)는 `design_approval` · `budget` · `risky_command` 와 system 이 올리는 `question` 이다. 판단 결재(직원이 도구로 올린다)는 `task_proposal` · `hire` · `structure_choice` · `pre_approval` · `final_review` 와 직원이 올리는 `question` 이다 (전제 5).
- `title` 은 한 줄(80자 이내)이다. 푸시 제목으로 그대로 쓴다 (N-1).
- 만들면 `inbox.created`(§7.4)를 내고 푸시를 보낸다 (즉시 등급, §5.9). 채용 권장(`hire_recommendation`)만 조용히 보낸다 (§5.9).
- 채용 권장은 열 번째 종류다. system 이나 재직 중인 인사 직원이 올리고, 작업도 런도 없다. 결정은 `interview` · `skip` 이고 알림에서는 결정할 수 없다. 결정 요청의 검사 순서(아래)는 같다 — 3 에서 거르고, 4 의 후속 조건은 없다.
- 결정 통로는 `POST /studio/inbox/{id}/decide`(§6.4) 하나다. owner 만 부른다.

결정 요청의 본문:

| 필드 | 뜻 | 저장하는 곳 |
|---|---|---|
| `decision` | §5.2 의 값 | `inbox.decision` |
| `comment` | 코멘트. 비워도 된다 (필수인 결정은 §5.2) | `inbox.decision_comment` |
| `data` | 구조가 있는 결정 내용 (§5.2 의 data 열) | `inbox.decision_json` |
| `via` | `app` · `notification` · `dashboard`. 기본 `app` | `decision_json.via` |

검사 순서와 오류 (HTTP 상태는 §6.3):

1. 결재가 없다 → `not_found`.
2. `status` 가 `pending` 이 아니다 → `already_decided`. 응답에 지금의 결재를 싣는다 — 앱이 "이미 처리됨"을 보여 준다 (A-9 완료 기준). `cancelled` 인 결재도 이 코드다.
3. `decision` 이 그 종류의 값이 아니다, 필수 코멘트나 `data` 가 없다, `via = notification` 인데 알림에서 결정할 수 없는 종류다 → `invalid_decision`.
4. 종류별 후속 동작의 조건(§4.3 의 "언제")을 못 채웠다 → `precondition_failed` 등 §4.5 의 코드.
5. 결정을 쓴다: §3.13 의 `UPDATE … WHERE id = ? AND status = 'pending'`. 바뀐 행 수가 1이 아니면 `already_decided` 다 (2와 5 사이에 다른 요청이 먼저 결정했다).

- 5와 작업 전이는 한 트랜잭션이다 (§4.5 실행 순서 1). T7 · T9 는 §4.5 의 예외 순서를 따른다 — 반영이나 되돌리기가 실패하면 결재는 `pending` 으로 남고 같은 결정을 다시 받는다.
- 두 요청이 동시에 와도 하나만 5를 통과한다. 진 쪽은 결정 값이 같아도 `already_decided` 다 (S-5 완료 기준).
- `cancelled` 로 닫는 경우는 셋뿐이다: 작업 없는 런의 결재가 가리키던 런이 studio 밖에서 풀렸을 때(§5.8 나), 인사가 초안을 다시 제출해 아직 `pending` 인 앞의 `hire` 결재를 대체할 때(§5.5), owner 가 `blocked` 작업을 취소해(T11, §4.3) 그 작업의 차단 결재를 닫을 때. 그때 `inbox.cancelled` 를 낸다. 그 밖에는 작업에 묶인 결재를 취소하지 않는다 — 작업 상태와 1:1 이라(§4.1) 결재만 닫으면 작업이 그 상태를 떠날 길이 없다. T11 은 결재를 닫는 같은 트랜잭션에서 작업이 `blocked` 를 떠난다. `proposed` 의 `task_proposal` 과 `review` 의 `final_review` 는 취소하지 않는다 — 거절(T2)과 되돌리기(T9)가 그 자리다 (F10).

### 5.2 종류별 결정 값

| 종류 | 올리는 쪽 | 결정 값 | 코멘트 필수 | `data` | 알림에서 결정 (N-3) |
|---|---|---|---|---|---|
| `task_proposal` | employee | `approve` · `reject` | `reject` | `approve`: `edits` (선택) | `approve` |
| `hire` | employee (인사) | `approve` · `reject` · `revise` | `revise` | `approve`: `edits` (선택) | `approve` |
| `design_approval` | system | `approve` · `revise` | `revise` | — | `approve` |
| `structure_choice` | employee | `choose` · `revise` | `revise` | `choose`: `option_id` (필수) | 불가 — 선택지를 골라야 한다 |
| `question` | employee · system | `answer` | 코멘트와 `option_id` 중 하나 | `option_id` (선택) | 불가 |
| `pre_approval` | employee | `approve` · `reject` · `revise` | `revise` | — | `approve` |
| `budget` | system | `approve` · `reject` | — | — | `approve` |
| `risky_command` | system (가드 훅) | `approve` · `reject` | — | — | 불가 (N-3) |
| `final_review` | employee (담당) | `pass` · `rework` · `revert` | `rework` | `judgment_marks` (선택) | 불가 (N-3) |
| `hire_recommendation` | system · employee (인사) | `interview` · `skip` | — | — | 불가 — 면담을 열지 넘길지 앱에서 정한다 |

- `edits` (`task_proposal`): `title` · `reason` · `spec` · `done_criteria` · `type` · `assignee_id` · `is_milestone` · `campaign_id` · `depends_on[]` 가운데 고칠 것만. 고친 값이 비면 `missing_fields`.
- `edits` (`hire`): `payload` 의 action 별 본문(§5.3)과 같은 키로, 고칠 것만.
- `option_id` 는 `payload.options[].id` 중 하나여야 한다. 아니면 `invalid_decision`.
- `judgment_marks`: `[{index, mark}]`. `index` 는 `payload.judgment_calls` 의 순번, `mark` 는 `should_have_asked`(물어봤어야 함) · `ok`(괜찮음) (H-6, A-10).
- "알림에서 결정"은 코멘트와 `data` 없이 되는 `approve` 만이다. 서버가 `via = notification` 으로 온 그 밖의 결정을 `invalid_decision` 으로 거부한다 (N-3 확정). 알림의 푸시 `data.action` 이 위 표의 마지막 열과 같다 — 열이 "되는 `approve`"인 종류는 `approve`, "불가"인 종류는 `open` 이다 (§5.9). `hire_recommendation` 은 이 둘 다 해당하지 않는다: 코멘트는 어느 결정에서도 선택이고 `data` 는 받지 않는다.

A-9 는 버튼을 "승인 / 반려 / 수정 요청" 셋으로 적는다. 종류마다 뜻이 달라 이렇게 대응시켰다. `[§12]`

| 종류 | 승인 | 반려 | 수정 요청 |
|---|---|---|---|
| `task_proposal` | `approve` (고쳐서 승인은 `edits`) | `reject` | 없음 — 고쳐서 승인하거나 거절한다. 거절된 제안은 직원이 새로 등록한다 (§4.3 T2) |
| `hire` | `approve` | `reject` | `revise` |
| `design_approval` | `approve` | 없음 — 설계를 버리는 결정은 없다 | `revise` |
| `structure_choice` | `choose` | 없음 | `revise` (선택지를 다시 내라) |
| `question` | `answer` | 없음 | 없음 |
| `pre_approval` | `approve` | `reject` | `revise` |
| `budget` | `approve` | `reject` | 없음 |
| `risky_command` | `approve` | `reject` | 없음 |
| `final_review` | `pass` (통과) | `revert` (되돌리기) | `rework` (재작업) |
| `hire_recommendation` | 없음 | `skip` (넘기기) | 없음 — `interview` (면담 시작) 이 승인에 가깝다 |

A-10 의 "후속 작업 만들기"는 결정이 아니다. owner 가 `parent_id` 를 넣어 작업을 등록하는 C2 다 (§6.4).

### 5.3 payload

`inbox` 컬럼에 있는 값(`task_id` · `run_id` · `project_id` · `requester_*`)은 payload 에 다시 넣지 않는다.

| 종류 | `task_id` · `run_id` | payload 필수 | payload 선택 |
|---|---|---|---|
| `task_proposal` | 제안된 작업 · NULL | `title` · `type` · `reason` · `spec` · `done_criteria` (등록 때의 사본) | `suggested_assignee_id` · `parent_id` · `depends_on[]` · `campaign_id` · `source_task_id` · `source_run_id` · `department_id` (맡을 파트. 채용 권장의 계기를 읽을 때만 쓴다, 아래 '작업이 맡을 파트') |
| `hire` | NULL · NULL | `action` · `draft_id` · `reason` + action 별 본문 (아래) | `source_inbox_id` · `supersedes_inbox_id` |
| `design_approval` | 작업 런이면 그 작업 · 그 런 | `stage` · `stop_reason` | `next_stage` · `done_stages[]` · `remaining_stages[]` · `design_summary` |
| `structure_choice` | 그 작업 · 작업 런 | `question` · `options[]` (2개 이상, 각 `{id, label, detail}`) | `options[].figure` · `recommended` · `fix_points` |
| `question` | 그 작업 · 작업 런 | `reason` · `body` | `options[]` (각 `{id, label}`) · `error` · `run_status` |
| `pre_approval` | 그 작업 · 작업 런 | `what` · `why` · `if_not` | `policy_ref` |
| `budget` | 작업 런이면 그 작업 · 그 런 | `scope` · `stage` · `spent_usd` · `limit_usd` · `stop_reason` | `next_stage` |
| `risky_command` | 그 작업 · 작업 런 | `command` · `tool` · `rule_id` · `reason` | `cwd` · `stage` |
| `final_review` | 그 작업 · 작업 런 | §5.4 | §5.4 |
| `hire_recommendation` | NULL · NULL (`project_id` 는 배치할 프로젝트) | `reason` · `role` · `model` · `effort` | 근거: `task_ids[]` 와 `numbers{}` 중 하나는 비어 있지 않다(필수). 파트: `department_id` 와 `part_name` 중 정확히 하나(필수). `part_name` 이면 `part_job` 필수, `part_relay` · `part_models` 선택. `signal_key` · `project_id` 선택 (서버가 `inbox.project_id` 로 채운다) |

- `task_proposal` 의 본문 필드는 등록 때의 사본이다. 결정 뒤의 기준은 `tasks` 행이다. `proposed` 작업은 담당을 갖지 않으므로(§3.10) 직원이 추천한 담당은 `suggested_assignee_id` 에만 있다.
- **채용 권장 (`hire_recommendation`, T4).** 인사가 "이 파트에 이 프로젝트로 사람이 더 필요하다"고 올리는 결재다. 서버는 모델을 부르지 않고 규칙으로 계기를 찾아(`studio/staffing.py`) 인사에게 '판단 요청'(`Hooks.staffing_ask`)을 하는 데까지 간다 — 인사의 판단 턴(`ChatTurns.judge`)과 두 도구(`hire_recommend` · `staff_request`, §8.4)는 T5 다. **채용 권장은 인사의 판단이라 보장이 아니다** — 계기가 있어도 인사는 아무것도 올리지 않을 수 있고, 그것이 정상이다. `numbers` 의 값은 숫자다. `department_id` 는 보관하지 않은 기존 파트이고, `part_name` 은 §5.5 의 새 파트 제안과 같은 형식(이름 · `part_job` · `part_relay` · `part_models`)이다. `payload.key` 는 서버가 쓰며, `create` 는 받은 payload 를 고치지 않고 복사해서 쓴다. `effort` 는 `low · medium · high · xhigh · max` 중 하나여야 하고(`pipeline.EFFORT_ORDER`), `model` 은 비어 있지 않은 자유 문자열이다(다른 프로바이더 이름도 받는다).
  - **계기 넷** (`staffing.Staffing`): (가) 작업이 등록 · 제안됐는데 그 파트에 재직 직원이 없다(`no_staff`) 또는 그 프로젝트에 배치된 직원이 없다(`no_placement`). (나) 한 직원 앞의 `backlog` 작업이 `Config.studio_hire_backlog_limit`(기본 5)보다 많다(`backlog`. 0 이하면 보지 않는다). (다) 직원이 알린 요청 — 입구 함수 `Staffing.request(employee_id, reason=, department_id=, project_id=, task_id=, role=)`(`request`. 도구는 `staff_request`, §8.4). (라) 일일 보고를 쓸 때(`stats.Reporter(sweep=)`) 시작 전 작업(`proposed` · `backlog`) 전체를 훑는다. 훑기와 `request(task_id=)` 는 작업의 `task_proposal` 결재를 상태와 무관하게 읽는다(`staffing.proposal_of`: `pending` 이 있으면 그것, 없으면 가장 최근 행) — 승인돼 `backlog` 가 된 작업도 payload 의 `department_id` 를 잃지 않는다. (가)(나)는 `Effects.staffing(task, item)` 이 `create_task` 끝(`Effects.after` 직전)에서 부른다.
  - **작업이 맡을 파트** 는 새 열 없이 읽는다: 제안 payload 의 `department_id`(선택 키) → 담당 직원의 파트(퇴사자 포함) → payload 의 `suggested_assignee_id` 직원의 파트. 셋 다 없으면 (가)는 계기가 아니다. 아트처럼 직원 없는 선택 파트는 작업이 그 파트를 `department_id` 로 가리킬 때만 계기가 된다. 인사 파트는 계기의 키가 되지 않는다.
  - **키** 는 `<department_id | new:<part_name>>:<project_id | ->` 다. (나)의 프로젝트는 그 직원의 배치 프로젝트이고, 없으면 작업의 프로젝트다.
  - **되풀이 막기** 셋: 같은 키(`payload.key` 또는 `payload.signal_key`)로 `pending` 인 권장이 있으면 올리지 않는다(`create` 는 `conflict`, `field = key`, `inbox` 에 열린 행). 넘긴(`skip`) 권장은 `Config.studio_hire_cooldown_days`(기본 3, 0 이하면 기다림 없음)일 동안 같은 키로 올리지 않는다(`precondition_failed`). 판단 요청도 같은 키로 그 기간에 한 번만 낸다(`hire_signals.asked_at`). 판단 요청은 훅을 부르기 전에 기록하고(동시에 온 계기 둘이 둘 다 묻지 않게), 훅이 `False` 를 돌려주거나(물을 인사가 없다) 예외를 내면 그 기록을 되돌린다(`gate` 의 `why = unheard`) — 인사가 듣지 못한 키가 기간 동안 묶이지 않는다. 훅이 `True` 나 `None` 이면 물은 것이다. 기본 훅은 `False` 다.
  - 새 파트로 권장하면 `key` 는 `new:<part_name>:…` 로 계기의 키와 달라진다. 인사가 그 사이를 잇는 값이 `signal_key` 다 (`hire_recommend` 의 인자. 판단 요청 글의 key 를 적는다).
- `hire` 의 `action` 은 셋이다. H-6 의 "정책 또는 지침 수정안 결재"를 당시의 9종(지금은 `hire_recommendation` 을 더해 10종, §5.1) 안에 넣으려고 `hire` 에 `action` 을 뒀다 — 셋 다 인사가 올리고, 작업과 런이 없고, 승인하면 파일을 쓴다. `[§12]` owner 확정 (§12.6 의 1): 이 배정으로 간다.

| `action` | 뜻 | 본문 필드 |
|---|---|---|
| `hire` | 새 직원 채용 (H-2) | `draft`: `{name, department_id, project_id, persona_md, guide_md, employee_yaml, first_message, part_name, part_job, part_relay, part_models}`. `project_id` · `first_message` 와 `part_*` 넷은 선택이다. `part_name` 이 비어 있지 않으면 새 파트 제안이다 — 그때 `part_job` 도 있어야 하고 `department_id` 는 서버가 정한 새 파트의 id 다 (아래 §5.5). `employee_yaml` 의 모양은 §9 |
| `guide_amend` | 기존 직원의 지침 · 설정 수정안 (H-6) | `target_employee_id` · `file`(`PERSONA.md` · `GUIDE.md` · `employee.yaml`) · `new_text` · `diff` · `base_sha256` |
| `policy` | 프로젝트 정책 초안 · 수정안 (H-6). 대상 프로젝트는 `inbox.project_id` 다 | `new_text` · `diff` · `base_sha256` (파일이 아직 없으면 빈 문자열) |

- `source_inbox_id` 는 수정안을 부른 `final_review` 결재다 (`judgment_marks`, §5.5). `supersedes_inbox_id` 는 `revise` 를 받았거나 대체된 앞 결재다.
- `design_approval` · `budget` 의 `stage` · `stop_reason` · `done_stages` · `remaining_stages` 는 런의 `StopNote`(`baton.py` 38–48)에서 옮긴다. `spent_usd` · `limit_usd` · `next_stage` 는 런 이벤트 `budget_exceeded`(`pipeline.py` 1586–1587: `spent_usd` · `limit_usd` · `next`) · `stage_budget_exceeded`(1728–1729: `limit_usd` · `spent_usd` · `reason`) · `awaiting_approval`(1786: `next` · `reason`)의 detail 에서 옮긴다. `scope` 는 `run` · `stage` 다. `done_stages` · `remaining_stages` 는 위 표대로 `design_approval` 에만 싣는다. `next_stage` 가 이벤트에 없으면(`stage_budget_exceeded`) `remaining_stages` 의 첫 단계 — 승인하면 다시 도는 그 단계 — 를 싣는다. 단계 예산 이벤트의 `spent_usd` 가 비어 있으면(러너가 쓴 비용을 알리지 못했다, 1711) 런의 누적 비용을 싣는다. `design_summary` 는 멈춘 단계의 마지막 `stage_finished` 이벤트의 `summary`(`pipeline.py` 1756. 단계 결과의 요약 그대로)를 `history.events(run_id)` 에서 읽고, 없으면 싣지 않는다 (S-5 에서 확인 · 구현, `studio/runs.py`).
- `structure_choice` 의 `options[].figure` 는 저장될 때 첨부 참조(§6.6 의 `{name, path, mime, size}`. 파일은 `attachments/<결재 id>/` 에 둔다) 또는 `{mermaid}` 이고, 그림이 없으면 `null` 이다. 직원이 넘기는 모양(`""` · `{path}` · `{mermaid}`)은 §8.4 이고, 파일이면 서버가 §6.6 '직원의 첨부'대로 검사해 `attachments/<결재 id>/` 로 복사한다 (S-11 ② 확정. 결재 id 는 전이 전에 서버가 정해 `Inbox.create(item_id=)` 로 넘기고, 전이가 실패하면 그 폴더를 지운다). 저장된 `figure` 는 `{name, path, mime, size}`(`mermaid` 가 같이 왔으면 그것도 남는다)다. `recommended` 는 `options[].id` 중 하나, `fix_points` 는 A-9 의 "다음 요청 때 고칠 곳"이다.
- `pre_approval` 의 셋은 H-6 의 "무엇을 · 왜 · 안 하면 어떻게 되는지"다. `policy_ref` 는 직원이 근거로 든 정책 · 지침의 줄이다.
- `risky_command` 의 `command` 는 명령 원문이다 (A-9). `rule_id` 는 §10.4 의 다섯 값 가운데 하나이고 `tool` 값은 §10.2 다. 셋 다 훅이 보낸 `POST /studio/guard/check` 의 요청에서 온다 (§6.4).

`question` 의 `reason`:

| `reason` | 올리는 쪽 | 계기 (§4.3 T4) | 서버가 넣는 `options` (잠정, S-5) |
|---|---|---|---|
| `asked` | employee | `ask_owner(kind = question)` | 직원이 준 것 |
| `run_failed` | system | 런이 `failed` 로 끝남. `error` 에 `RunState.error`. 진행 스레드가 런을 진행시키지 못하고 예외로 돌아왔으면 그 예외 문구다 — 그 진행 중에 엔진이 런의 실패를 기록했으면 `RunState.error` 그대로다 (§4.7) | `retry`(다시 시도) · `wrap_up`(지금까지 한 것으로 검수 요청) |
| `run_cancelled` | system | 런이 `cancelled` 로 끝남 (owner 의 중지 포함, §6.4. 런이 이미 멈춘 줄 선 작업의 `stop` 은 런 상태가 `cancelled` 가 아니어도 이 사유를 쓴다) | `retry` · `wrap_up` |
| `no_review_request` | system | 런이 검수 요청 없이 `done` | 없음 |
| `exchange_limit` | system | 직원 간 왕복 한도 (§4.6) | `continue`(다시 세고 계속) · `wrap_up` |
| `editor_unresponsive` | system | 에디터 무응답 (M-7) | `retry` |

M-7 의 "결재로 알린다"와 S-9 의 "`ask_owner` 결재로 멈춘다"는 따로 종류를 두지 않고 `question` 으로 받는다 (§4.3 T4 표). `[§12]`

### 5.4 최종 검수 패키지

`final_review` 의 payload 에는 T6 순간에 정해진 것만 넣는다. 런이 멈춘 뒤 달라지는 것은 저장하지 않고 `GET /studio/inbox/{id}` 가 그때 계산해 `package` 로 준다. T6 뒤에도 런이 남은 단계를 돌아(§4.3 T6) patch · 비용 · 가드 기록이 바뀌기 때문이다. `[§12]`

payload (저장):

| 필드 | 필수 | 뜻 |
|---|---|---|
| `summary` | ● | 한 줄 요약 |
| `checklist[]` | ● | 완료 기준별 `{criterion, status, note}`. `status` 는 `verified_auto` · `verified_manual` · `unverified` (S-11) |
| `evidence[]` | | `{kind, ref, caption}`. `kind` 는 `screenshot` · `log` · `file` · `url`. 저장되는 `ref` 는 `url` 이면 문자열, 나머지 셋이면 첨부 참조(§6.6 의 `{name, path, mime, size}`)다 — T6 때 서버가 `attachments/<결재 id>/` 로 복사한 사본이라 워크스페이스가 정리된 뒤에도 남는다. 직원이 넘기는 `ref` 는 문자열(파일 경로 또는 URL)이다 (§8.4) |
| `judgment_calls[]` | | 묻지 않고 처리한 판단 `{text, why}` (직원 자진 기재, S-12) |
| `structure_choice_inbox_id` | | 하네스 게이트가 근거로 본 결재 (§4.3 T6) |
| `exchange_count` | ● | T6 때의 값 |

`package` (GET 때 계산):

| 필드 | 출처 |
|---|---|
| `run` | `{status, error, stop}` — `RunState` 와 `StopNote`. 런이 어떻게 끝났는지 (§4.7 의 `review` 행). 결재가 가리키는 런(`inbox.run_id`)을 엔진이 읽지 못하면 NULL 이다 — 그때 아래의 목록은 빈 배열, `changes` 의 `status` · `workspace_mode` · `patch_url` 은 NULL, `cost.usd` 는 0, `can` 의 `pass` · `revert` 는 `ok = false` 다 (S-12) |
| `run_stopped` | 작업 런이 슬롯을 놓았는가 (§4.6) — 큐가 그 작업의 슬롯을 쥐고 있지 않고 런이 `running` 이 아니다. `false` 면 통과 · 되돌리기를 받지 않는다 |
| `changes` | `{status, workspace_mode, files[], patch_url}` — `RunState.changes_status` · `changes`, patch 는 `/studio/runs/{run_id}/patch` (§6.4). `files[]` 는 `result.patch` 의 `diff --git` 블록마다 `{path, status, insertions, deletions, binary}` 이고 `status` 는 `added` · `modified` · `deleted` 다. git 이 따옴표와 8진수로 적은 경로(공백 · 한글)는 풀어 싣는다. `inplace` 는 patch 에 없는 journal 의 `binary[]`(아래)도 `binary = true` 로 더한다 (S-12) |
| `assets[]` | 애셋 변경 목록, 각 `{path, asset, before_bytes, after_bytes, restorable, source}`. `inplace` 는 journal 이 diff 하지 않고 적어 둔 `RunState.changes.binary[]` 그대로다 — 바이너리이거나 원본을 알 수 없는 파일이고, `asset` 은 확장자가 `journal.ASSET_EXTS` 에 드는가다. journal 은 런 폴더(`runs/<run_id>/`)의 `journal.json.gz` · `journal.meta.json` · `baseline/` · `touched.jsonl` 이고, 목록은 런이 멈출 때 `JournalWorkspace.collect`(`journal.py`)가 만들어 `run.json` 에 남는다. `copy` 는 journal 이 없어 patch 의 파일 중 바이너리이거나 애셋 확장자인 것을 같은 키로 싣는다 — 크기는 NULL, `source` 는 `snapshot`. `copy` 의 스냅샷은 `Content` 폴더와 `*.uasset` · `*.umap` 을 빼므로(`workspace.DEFAULT_EXCLUDES`) 그런 애셋은 `copy` 런의 목록에 나오지 않는다 (S-12 확정) |
| `screenshots[]` | 첨부 참조(§6.6) 목록 — `evidence` 의 `screenshot` 과 M-7 이 방에 올린 것. 파일 evidence 는 S-11 까지 도구가 받지 않고(§8.4) M-7 도 아직 없어 지금은 빈 배열이다 (S-12) |
| `harness_checks[]` | S-11 ② 의 판정 `[{id, status, note, data}]` (`studio/gate.py` 의 `checks`). `status` 는 `passed` · `failed` · `warning` · `info`. 항목은 다섯이다 — `structure_choice`(feature 의 코드 변경에 결정된 구조 선택이 있나. `data` = `{inbox_id, option_id, code_files[≤20]}`. 코드는 없고 블루프린트(`.uasset` · `.umap`)만 바뀐 feature 는 `warning` 이고 `data` 에 `asset_files[≤20]` 가 더해진다) · `checklist`(표기별 개수. `unverified` 가 있으면 `warning`) · `skills`(스킬 호출 이름, `info`) · `mcp`(호출 · 오류 수, 오류가 있으면 `warning`) · `guard_warnings`(감사 로그의 `verdict != allow`, 있으면 `warning`). 뒤의 셋의 원료는 `studio/runlog.py` 의 런별 기록(`runlog.snapshot`, §10.7 '런별 모음')이고, 그 기록을 못 읽으면 `info` 와 '런 기록을 읽지 못했다'다. 엔진이 읽지 못하는 런이면 빈 배열이다 |
| `out_of_scope[]` | 담당 밖 변경, 각 `{path}`. 설정 `studio_scopes`(파트 id → glob 목록, `config.py`)에서 담당 직원의 파트 목록을 찾아, `files[]` · `assets[]` 의 경로 중 어느 패턴에도 맞지 않는 것을 싣는다. 경로는 프로젝트 폴더 기준 `/` 표기로 견주고 대소문자를 가리지 않는다. `/` 로 끝나는 패턴은 그 폴더 아래 전부다. 목록이 없는 파트는 판정하지 않고 빈 배열이다 (S-12 확정) |
| `cost` | `{usd}` — `RunState.cost_usd`. 토큰은 `/studio/runs/{run_id}/tokens` |
| `incidents[]` | 검수 대상 런(`tasks.run_id`)이 건너뛴 단계의 `run` 행(`detail_json.event` 가 `stage_skipped`. T6 앞의 것도 싣는다, §4.7)과, T6 뒤의 `task_events` 중 `guard` · `limit` · `run` 행 (§4.5 의 표가 "검수 패키지에 싣는다"고 한 것) |
| `can` | `{pass, rework, revert}` 각각 `{ok, why}` — §4.3 T7 · T8 · T9 의 조건을 지금 채우는가. `why` 는 막는 이유이고 `ok` 면 빈 글이다. 결재가 `pending` 이 아니면 셋 다 `false` 다. 읽을 때의 계산이라 결정 때의 검사를 대신하지 않는다 (S-12) |
| `followups[]` | 이 작업을 `parent_id` 로 하는 작업, 각 `{id, title, status, type, assignee_id, is_milestone}` (§8.4 `task_list` 의 줄과 같다) |

`incidents[]` 의 각 줄은 `{id, at, kind, reason, detail}` 이고 `task_events` 의 순서다. "T6 뒤"는 이 결재를 만든 T6 전이 행 뒤부터 그 작업이 `review` 를 벗어나는 전이 행 앞까지다 (S-12).

패키지는 `studio/review.py` 가 계산하고 아무것도 쓰지 않는다. 엔진은 `server.py` 가 `api.use_engine` 으로 넘긴다.

`task.updated`(`change = run`)는 `task_events` 에 `run` 행이 생길 때 나간다 (§7.5) — 런이 멈출 때 건너뛴 단계를 적는 행(`work.py` 의 `stopped`), 런을 만들지 못했을 때의 행(`tasks.py`), 에디터 무응답으로 런을 끊었을 때의 행(`work.py` 의 `editor_lost`)이다. 앱은 그때 패키지를 다시 읽는다. 런이 멈출 때마다 이 이벤트를 내는 것은 S-12 가 넣지 않았다 — 건너뛴 단계 없이 멈춘 런에서는 나가지 않는다. 그 런이 멈춘 것을 따로 알리는 `task.updated` 는 없고, `run_stopped`(위 표)는 패키지를 읽을 때 계산하는 값이다. 건너뛴 단계 없이 멈춘 런을 스트림으로 알릴지는 검수 화면 작업이 정한다 (잠정, A-10).

### 5.5 결정 → 후속 동작

| 종류 | 결정 | 작업 전이 | `delivery` | 그 밖의 후속 동작 |
|---|---|---|---|---|
| `task_proposal` | `approve` | T1 | `none` | `edits` 를 반영한다(`edited` · `assigned` · `deps` 이벤트). `edits.depends_on` 까지 T1 과 한 트랜잭션이라, 전이가 거부되면 선행 관계도 남지 않는다. 작업방을 만든다. 제안한 직원의 1:1 방에 시스템 글 |
| | `reject` | T2 | `none` | 코멘트를 `reason` 에. 제안한 직원의 1:1 방에 시스템 글 |
| `hire` | `approve` | — | `none` | `action` 별 동작 (아래) |
| | `reject` | — | `none` | 아무것도 쓰지 않는다. 인사의 1:1 방에 시스템 글(초안 id). 인사 턴은 넣지 않는다 — 인사는 다시 올리지 않고 owner 의 다음 글을 기다린다 |
| | `revise` | — | `none` | 인사의 1:1 방에 시스템 글(코멘트 · 초안 id) + 인사의 채팅 턴. 인사가 그 `draft_id` 로 고쳐 다시 제출하면 새 결재가 생긴다 |
| 차단 6종 | 모든 값 | T5 | `pending` | 결정문(§5.6)을 `run_id` 의 런에 넣고 잇는다 (§4.7). 작업방에 시스템 글. 종류별 덧붙임은 아래 |
| `final_review` | `pass` | T7 | `none` | 반영 · 커밋 (§4.3). 그 작업에 남은 `delivery = 'pending'` 을 `none` 으로, 쓰지 않은 1회 허용을 `void` 로 (§10.5) |
| | `rework` | T8 | `pending` | 코멘트가 결정문이다. 런이 `done` 이면 같은 런을 다시 열어 그 대화에 넣는다 (§4.7 의 `done` 행. S-12 확정) |
| | `revert` | T9 | `none` | 변경 되돌림 (§4.3). 남은 `pending` 을 `none` 으로, 쓰지 않은 1회 허용을 `void` 로 (§10.5) |
| `hire_recommendation` | `interview` | — | `none` | 결정을 쓰고(`inbox.decided`), 인사의 1:1 방에 권장 내용(사유 · 파트 또는 새 파트 · 역할 · 모델 · 노력치 · 프로젝트 · 관련 작업 · 수치)이 실린 시스템 글을 올리고 인사의 채팅 턴을 넣는다 (T5). 인사는 거기 적힌 것을 다시 묻지 않고 남은 것만 물어 기존 채용 면담(§5.5 `hire`)을 잇는다. 인사나 그 방이 없으면 시스템 방에 글만 쓴다. 작업도 런도 없다 |
| | `skip` | — | `none` | 결정만 쓴다(방에 글도 턴도 없다). 같은 키로는 `studio_hire_cooldown_days` 일 동안 다시 올리지 않는다 (§5.3) |

- 시스템 글은 `author_kind = 'system'` 이고 `meta` 에 `{event: 'inbox.decided', inbox_id, task_id}` 를 넣는다 (§7.6). 직원은 다음 턴에 방의 새 글로 결정을 읽는다 (잠정, S-5).
- **런을 이을 수 없는 결정 값은 없다.** 차단 6종은 결정 값이 무엇이든 T5 와 `delivery = 'pending'` 이다 — 거절도 "그 방법으로는 하지 마라"는 지시로 직원에게 간다. §3.13 과 §4.3 T5 가 §5 로 미룬 예외는 두지 않는다. 결재의 결정 값으로는 작업을 종결하지 않는다 — 런을 끊고 끝내면 `working` 작업이 주인 없이 남는다. 작업을 그만두는 것은 결정이 아니라 owner 의 `cancel` 이고(T11, §6.4), 그때 이 결재는 결정되지 않은 채 `cancelled` 로 닫힌다 (§5.1). `done` 으로 끝난 런도 다시 열어 잇는다 (§4.7 의 `done` 행). 이을 수 없는 것은 변경을 이미 결정한 런뿐이고, 그런 런의 재작업 · T5 는 `precondition_failed` 로 거부한다 (§4.3 의 `failed[]` 표. S-12 확정).

**`hire` 승인의 `action` 별 동작.**

| `action` | 동작 | 실패하면 |
|---|---|---|
| `hire` | (1) `emp_` id 를 정하고 `employees/<id>/` 에 `PERSONA.md` · `GUIDE.md` · 빈 `MEMORY.md` · `employee.yaml` 을 쓴다 → (2) 한 트랜잭션에서 (`draft.part_name` 이 있으면 먼저 새 파트 행과 파트방, 아래) `employees` 행, 1:1 방, `room_members` 행, 파트방의 `room_members` 행을 만들고 결재를 `decided` 로 → (3) 직원 폴더의 첫 git 커밋 (H-3) → (4) 1:1 방에 그 직원 이름으로 첫 인사 글(`draft.first_message`, 없으면 고정 문안. 모델을 부르지 않는다) | (1)이 실패하면 결정하지 않는다. (2)가 실패하면 쓴 폴더를 지우고 결재는 `pending` 으로 둔다. 이름이 겹치면 `conflict` |
| `guide_amend` | 대상 직원 폴더의 `file` 을 `new_text` 로 쓰고 커밋한다 (H-3). `employee.yaml` 이면 §9 의 검사를 거친다 | 지금 파일의 SHA-256 이 `base_sha256` 과 다르면 `precondition_failed` (제출 뒤에 파일이 바뀌었다). 결재는 `pending` |
| `policy` | `runs/studio/projects/<id>/POLICY.md` 를 `new_text` 로 쓴다 (§2.2) | 위와 같다 |

**새 파트를 함께 제안하는 `hire` (T3 확정).** `draft.part_name` 이 비어 있지 않으면 결재 제목이 `채용: <이름> — 새 파트 <part_name> 를 만든다` 이고 payload 의 `draft` 에 `part_name` · `part_job` · `part_relay` · `part_models` 가 함께 실린다 — owner 는 이 제목과 payload 로 파트가 생기는 것을 본다. 승인 때 검사는 `_check_hire` 가 한다: 같은 id 또는 같은 이름(보관 포함)의 파트가 있거나 `part_job` 이 비었거나 `part_models` 가 매핑이 아니거나 기본값이 §9.4 에 걸리면(없는 릴레이는 `relay_unknown`) `precondition_failed` 이고 아무것도 만들지 않는다 — 결재는 `pending` 이다. 통과하면 한 트랜잭션에서 파트 → 파트방 → 직원 → 1:1 방 → 구성원 → 첫 인사 글 순으로 만들고, 파트방의 `room.updated` 는 구성원이 든 채로 한 번 낸다 (파트 전용 이벤트는 없다, §7.4). 반려 · 수정 요청은 결정만 쓴다. 이 검사는 승인 트랜잭션 안에서 한 번 더 한다. `edits.draft` 로 `part_*` 도 고칠 수 있다. 직원을 기존 파트에 배치하는 REST(H-3 ②)는 만들지 않는다.

`hire` 의 동작은 확정이다 (H-2 · H-6). 커밋은 H-3 이 정했다: `hire` 의 (3)과 `guide_amend` 의 커밋은 결정 트랜잭션 뒤에 `Hooks.commit` 이 직원 폴더의 자체 git 에 한다 (`runs.EngineHooks.commit` → `studio/employee_git.py`). 메시지는 `hire: <이름> (<직원 id>)` · `guide_amend: <file> (<결재 id>)` 다. 커밋이 실패해도 결정과 파일은 그대로이고 로그만 남는다 — 그 변경은 그 폴더의 다음 커밋에 함께 담긴다 (§9.6). 인사가 아직 `pending` 인 결재의 초안을 다시 제출하면 앞 결재를 `cancelled` 로 닫고 새 결재에 `supersedes_inbox_id` 를 적는다. `revise` 로 결정된 결재의 초안을 다시 제출해도 새 결재에 그 결재의 id 를 적는다. `hire` 의 `reject` · `revise` 시스템 글은 마지막 줄이 `초안 id: <draft_id>` 다 (H-2 확정) — 인사의 대화가 새로 열린 뒤에도 그 글만 보고 같은 초안을 `employee_draft(draft_id=…)` 로 고칠 수 있다.

**차단 6종의 덧붙임.**

| 종류 | 덧붙임 |
|---|---|
| `design_approval` | `approve` · `revise` 모두 `engine.approve` 로 다음 단계로 간다. 멈출 때 엔진이 이미 단계 순번을 올렸기 때문이다(`pipeline.py` 1777). `revise` 의 코멘트는 다음 단계가 시작할 때 지시로 들어간다(`_merge_notes`, 1594). owner 가 설계 단계를 다시 돌리게 하는 기존 경로는 없다 (S-5 에서 확인): 엔진에서 순번이 뒤로 가는 곳은 단계의 `on_retry` 로 보내는 두 줄뿐이고(1771 — 단계 결과가 `retry` · `fail` 일 때, 1532 — 그렇게 `failed` 로 끝난 런을 재개할 때), 단계를 골라 되돌리는 메서드나 엔드포인트는 없다. 그래서 `revise` 도 다음 단계로 간다 |
| `structure_choice` | `choose` 로 결정된 결재만 T6 하네스 게이트의 근거다 (§4.3 T6). `revise` 로 닫힌 것은 근거가 아니다 |
| `question` | `reason = exchange_limit` 이면 `exchange_count = 0` (§4.3 T5). `editor_unresponsive` 면 재개 직전에 에디터를 다시 검사한다 (§4.7). `option_id = wrap_up` 이면 결정문이 "검수 요청을 올려라"가 된다 (§5.6) |
| `pre_approval` | `reject` 도 T5 다. 그 변경 없이 진행하라는 지시가 간다 |
| `budget` | 아래 |
| `risky_command` | `approve` 는 그 작업(`inbox.task_id`)의 그 명령에 1회 허용을 남긴다. 쓰는 것은 그때의 작업 런이다 — 토큰의 `run` 이 `tasks.run_id` 와 같은 런 (§6.4). 런이 `done` 으로 끝나 새 런이 `tasks.run_id` 를 바꿔도(§4.7) 새 작업 런이 그 허용을 쓴다. 한 번 쓰면 없어진다. T8 뒤에도 남고, T9 · T11 은 쓰지 않은 허용을 버리며, T7 뒤에는 쓸 작업 런이 없다(F9). owner 확정 (§12.6 의 2): T8 뒤에도 허용이 남는다. 허용은 서버가 갖고, 훅은 직접 읽지 않고 `POST /studio/guard/check` 로 묻는다 (§6.4 가드의 분기 1). 저장 자리(`decision_json.grant`), 키의 형식, 같은 명령으로 보는 기준은 §10.5 다. `reject` 는 결정문만 간다 |

**`budget` 의 `approve` 와 `reject`.** 엔진은 예산에서 멈춘 런을 `approve` 없이는 넘기지 못한다 — `resume` 으로 이으면 같은 검사(`pipeline.py` 1583)에 다시 걸린다. `approve` 는 런 예산이면 지금까지 쓴 만큼을 한도에 더하고(661–663), 단계 예산이면 그 단계 한도를 2배로 올린다(664–667). 그래서 두 결정 모두 `interject` → `engine.approve` → `advance` 로 잇고, 다른 것은 결정문뿐이다. `reject` 의 결정문은 "더 쓰지 말고 지금까지 한 것으로 검수 요청을 올려라"다. 이것은 지시이지 보장이 아니다 — 엔진은 한도 하나만큼을 더 쓸 수 있게 열어 준다. 런을 끊지 않는 이유는 위의 "이을 수 없는 결정 값은 없다"와 같다. 검수로 보내야 owner 가 통과 · 재작업 · 되돌리기를 고를 수 있다. `[§12]` owner 확정 (§12.6 의 3): 예산 반려는 런 중단이 아니라 이 지시로 한다.

**`judgment_marks`.** `final_review` 의 어느 결정에든 실을 수 있다. `should_have_asked` 가 하나라도 있으면 결정 뒤에 인사의 1:1 방에 시스템 글을 쓰고 인사의 채팅 턴을 넣는다. 글에는 인사가 수정안을 쓰는 데 필요한 것이 모두 실린다 — 작업 제목 · id, 담당 직원 이름 · id, 프로젝트 id, 표시된 판단 원문(`text` · `why`; `ok` 는 뺀다), 이 검수 결재의 id 와 그것을 `source_inbox_id` 에 적으라는 안내, 지침이면 `employee_draft(target_employee_id, file="GUIDE.md", guide_md)` → `employee_submit`, 프로젝트 전체의 기준이면 `policy_draft(project_id, policy_md, reason, submit=True)` 를 쓰라는 줄. 대화가 새로 열려도 인사는 그 글만 보고 따라갈 수 있다. 인사가 수정안을 올리면 `hire` 결재(`guide_amend` · `policy`)가 되고 `source_inbox_id` 가 이 검수 결재를 가리킨다 (H-6 의 4; 서버는 그 id 가 실제 `final_review` 인지 검사하지 않고 payload 에 그대로 보존한다). 승인하면 대상 파일이 바뀌고 직원 폴더에 커밋되며, 직원의 다음 턴이 이어 가는 대화이면 새 `GUIDE.md` 가 새 글 뒤에 한 번 실린다 (`chat.py` 의 `_guide_note`). 서버를 다시 시작한 뒤 첫 이어 가는 턴은 기억이 없으므로 지침을 한 번 다시 싣는다 (`_policy_note` 와 같다). 작업 런은 시작할 때마다 디스크의 `GUIDE.md` 를 읽어 싣는다 (`work.py` 의 `_goal`). 인사 직원은 있는데 1:1 방이 없으면 '인사 직원 <이름>의 1:1 방이 없어 인사에게 알리지 못했다' 한 줄로 시스템 방에 남긴다. 인사 직원이 없으면 같은 글에 '인사 직원이 없어 수정안은 올라가지 않는다' 한 줄을 더해 시스템 방에만 남긴다 — 턴도 수정안 결재도 없고, 나중에 인사가 채용돼도 소급하지 않으며, owner 가 REST 로 지침 · 정책을 직접 고친다 (H-6 확정).

### 5.6 결정문

결정문은 차단 6종과 `rework` 의 결정을 직원에게 전하는 글이다. `engine.interject(run_id, 결정문)` 으로 넣는다 (§4.7). 같은 글을 작업방의 시스템 글로도 남긴다.

```
[결재 inb_1a2b3c4d · 사전 승인 · 승인]
올린 변경이 승인됐다. 올린 범위 안에서 진행하라.
코멘트: <decision_comment — 있을 때만>
```

| 종류 | 결정 | 지시 문장 (잠정, S-5 가 상수로 둔다) |
|---|---|---|
| `design_approval` | `approve` | 설계가 승인됐다. 다음 단계를 진행하라. |
| | `revise` | 설계를 코멘트대로 고쳐서 진행하라. 무엇을 고쳤는지 작업방에 적어라. |
| `structure_choice` | `choose` | 구조는 선택지 `<id>`(`<label>`)로 정해졌다. 이 구조로 진행하라. |
| | `revise` | 선택지를 다시 내라. 코멘트를 반영해 `ask_owner(kind = choice)` 를 다시 올려라. |
| `question` | `answer` (`asked`) | 질문의 답이다: `<고른 선택지의 label>`. |
| | `answer` (`run_failed` · `run_cancelled` · `editor_unresponsive`, `retry`) | 멈췄던 자리에서 이어서 하라. |
| | `answer` (`exchange_limit`, `continue`) | 왕복 수를 다시 셌다. 이어서 하라. |
| | `answer` (`no_review_request`) | 검수 요청 없이 끝났다. 남은 일을 마치고 `task_request_review` 를 올려라. |
| | `answer` (`wrap_up`) | 새 일을 벌이지 말고 지금까지 한 것으로 `task_request_review` 를 올려라. 검증하지 못한 완료 기준은 `unverified` 로 적어라. |
| `pre_approval` | `approve` | 올린 변경이 승인됐다. 올린 범위 안에서 진행하라. |
| | `reject` | 올린 변경은 하지 않는다. 그 변경 없이 할 수 있는 범위로 진행하고, 못 하면 `ask_owner` 로 다시 물어라. |
| | `revise` | 올린 변경을 코멘트대로 바꿔서 진행하라. |
| `budget` | `approve` | 예산이 늘었다. 이어서 진행하라. |
| | `reject` | 예산을 더 쓰지 않는다. `wrap_up` 과 같은 문장. |
| `risky_command` | `approve` | 이 명령을 한 번 실행해도 된다: `<command>`. 다른 명령은 다시 결재를 받는다. |
| | `reject` | 이 명령은 실행하지 않는다: `<command>`. 다른 방법으로 진행하거나 `ask_owner` 로 물어라. |
| `final_review` | `rework` | 검수에서 재작업이 나왔다. 코멘트를 반영하고 다시 `task_request_review` 를 올려라. |
| `hire_recommendation` | `interview` | 채용 권장에 대해 owner 가 면담을 열기로 했다. 권장의 근거를 들어 채용안을 의논하라. |
| | `skip` | 채용 권장을 넘겼다. 같은 파트·프로젝트로는 한동안 다시 올리지 않는다. |

채용 권장의 결정문은 런이 아니라 인사에게 간다 (T5, 위 표의 `interview` 행). 아래 한 줄은 그 종류의 머리 형식이다: `[결재 <id> · 채용 권장 · 면담 시작]`.

코멘트만 있고 선택지가 없는 `answer` 는 지시 문장 없이 머리와 코멘트만 보낸다.

### 5.7 담당이 아닌 구성원이 `ask_owner` 를 불렀을 때

§4.3 T4 가 미룬 것이다. 작업방의 읽기 전용 턴에서 담당이 아닌 구성원이 `ask_owner` 를 부르면:

- 작업이 T4 된다 (`actor` = 부른 직원). 조건은 T4 와 같다 — 작업이 `working` 이고 부른 직원이 그 작업방 구성원이다.
- `inbox.requester_id` 는 부른 직원이고, `inbox.run_id` 는 항상 작업 런(`tasks.run_id`)이다. 부른 직원의 읽기 턴 런이 아니다.
- 부른 직원의 턴은 "이제 턴을 끝내라"를 받고 끝난다. 작업 런이 돌고 있으면 서버가 기다리지 않고 바로 `RelayEngine.cancel` 로 끊는다 — 그 런은 턴을 끝내라는 응답을 받지 않았다. 바로 끊기와 대기 타이머(§8.3)를 가르는 기준은 담당 여부가 아니라 "부른 턴의 런이 작업 런(`tasks.run_id`)인가"다: 같으면 타이머, 다르면 바로 끊는다. 한 호출에서 둘이 겹치지 않는다.
- 결정문은 작업 런과 작업방 글 양쪽에 간다. 부른 직원은 다음 턴에 방의 글로 읽는다. 부른 직원의 턴을 서버가 따로 넣지는 않는다 (S-9 확정).

작업이 없는 턴(1:1 방 · 파트방의 채팅)의 `ask_owner` 는 결재를 만들지 않고 `no_task_context` 를 돌려준다 (§8.4). 채팅에서는 방에 그대로 묻는다.

### 5.8 런 이벤트와 결재

S-5 의 "기존 런 승인 대기 · 예산 초과도 결재로 들어오게"와 §4.7 이 미룬 "studio 밖에서 작업 런을 조작했을 때"를 정한다.

**(가) 작업 런.** §4.7 의 표가 처리한다. 진행 스레드가 `advance` 에서 돌아와 런이 `awaiting_approval` 이고 작업이 `working` 이면 `StopNote.kind` 로 종류를 정한다. S-8 의 진행 스레드가 생기기 전에는 (나)의 수신기가 이 두 행을 맡는다 — `advance` 가 런을 놓은 직후(`after_advance`), 런이 `tasks.run_id` 에 있고 작업이 `working` 이며 줄 서 있지 않으면 system 이 T4 를 한다(`title` 은 작업 제목). 그 밖의 작업 상태에서는 아무것도 만들지 않는다. 진행 스레드가 생긴 뒤에도 이 갈래는 수신기에 남는다 (S-8 확정): §4.7 의 걸음 3과 재시작 복구의 걸음 1(§4.8)은 같은 코드를 다시 부르고(`runs.file_stop`), 작업이 그때 이미 `blocked` 면 아무것도 만들지 않으므로 결재가 두 번 오르지 않는다. 결정문 넣기는 `EngineHooks.interject` 가 한다 — 런이 `running` 이면 넣지 않고 `pending` 으로 둔다 (§4.7 "런을 이을 때").

| `StopNote.kind` | 쓰는 곳 | 결재 | `scope` |
|---|---|---|---|
| `budget` | `pipeline.py` 1588 | `budget` | `run` |
| `stage_budget` | `pipeline.py` 1730 | `budget` | `stage` |
| `awaiting_approval` | `pipeline.py` 1787 | `design_approval` | — |

`stage_budget` 는 `baton.py` 41 의 주석에 없지만 코드가 쓰는 다섯 번째 값이다 (1730행, 읽음). §4.7 과 §11.2 의 `kind` 목록에도 이 값을 넣었다. `RunState.pending_budget_stage`(1727)로도 구분된다.

**(나) 작업 없는 런.** `tasks.run_id` 에 없는 런 — 대시보드 · CLI · 기존 캠페인의 런과 studio 의 채팅 턴 런 — 이 승인 대기나 예산 초과로 멈추면 작업 없는 결재를 만든다. `[§12]` owner 확정 (§12.6 의 4): 이 런들도 결재함에 넣는다.

- 받는 자리: 엔진이 알림을 낼 때 부르는 `Notifier.__call__(run_id, kind, title, detail)`(`notify.py` 50)와 같은 자리에 studio 수신기를 하나 더 건다. 엔진은 `RelayEngine._event`(`pipeline.py` 374–380)에서 이벤트를 기록한 뒤, 종류가 `NOTIFY_KINDS` 에 있으면 `self.notifier(run.id, kind, 목표의 첫 줄 80자, detail)` 을 부르고 예외는 삼킨다(376–380). 그 자리에 `Fanout`(`notify.py`)으로 `Notifier` 와 수신기(`studio/runs.py` 의 `RunSink`)를 함께 건다 — 토스트는 그대로다 (S-5 에서 확인 · 구현).
- 만드는 때: 그 호출은 정지 기록과 저장보다 먼저다 (`budget_exceeded` 1586 → `_write_stop` 1588 → `save` 1591. 다른 두 정지도 같은 순서다). 그때는 `StopNote` 도 `awaiting_approval` 상태도 디스크에 없다. 그래서 수신기는 이벤트(kind · title · detail)를 기억만 하고, 결재는 `advance` 가 런을 놓은 직후에 만든다 — `advance` 의 `finally` 끝(1509)이 부르는 `RelayEngine._let_go` 가 런 잠금을 놓고 `after_advance(run_id)` 를 부른다 (S-5 가 더한 연결 지점. 인용한 줄 번호가 밀리지 않게 클래스 끝에 뒀다). 런을 놓은 뒤라야 뒤따르는 승인이 런 스레드의 마지막 `save` 에 덮이지 않는다 — `_write_stop` 은 먼저 저장한 뒤 인계 AI 를 부른다(1351–1353). 기존 대시보드의 `/runs/{id}/approve` 는 이 틈을 그대로 갖는다.
- 붙는 엔진: 서버 프로세스의 엔진뿐이다 (`server.py` 맨 아래의 `studio_runs.attach`). CLI 는 자기 프로세스의 엔진으로 런을 진행시키고(`cli.py` 668–672) studio DB 를 열지 않으므로(§3.1), CLI 가 진행시킨 런의 정지는 서버가 도는 동안 결재함에 들어오지 않는다 — 다음 서버 시작 때 채운다 (아래 "시작할 때". 잠정, S-5). studio 저장소가 열리지 않았을 때도 만들지 않는다 — 런과 토스트는 그대로 가고, 그 정지도 다음 시작 때 채운다.
- 대응: 이벤트 `awaiting_approval` → `design_approval`, `budget_exceeded` · `stage_budget_exceeded` → `budget` (`NOTIFY_KINDS`, `notify.py` 16–24). `task_id` 는 NULL, `requester_kind = 'system'`, `run_id` 는 그 런, `project_id` 는 런의 `repo` 가 `projects.repo` 에 있으면 그 프로젝트, `title` 은 세션 제목이다(없으면 목표의 첫 줄). 그 런에 `pending` 인 작업 없는 결재가 이미 있으면 — 알려지지 않은 채 풀린 앞 정지의 것이다 — `cancelled` 로 닫고 새로 만든다. 내용이 같으면 그대로 둔다 (잠정, S-5).
- 결정: `approve` 는 기존 `/runs/{id}/approve`(`server.py` 814–819)와 같다 — `engine.approve` 뒤 진행 스레드를 띄운다. `design_approval` 의 `revise` 는 코멘트를 `engine.interject` 한 뒤 같은 걸음이다. `budget` 의 `reject` 는 `engine.cancel` 이다 (작업이 없어 주인 없는 작업이 생기지 않는다). 결정과 함께 그 자리에서 처리하므로 `delivery` 는 `none` 이다. 잇는 것은 `Hooks.approve_run` · `cancel_run` · `advance_run`(`studio/inbox.py`. 엔진에 붙인 구현은 `studio/runs.py` 의 `EngineHooks`)이다. 엔진을 부르기 전에 런이 `awaiting_approval` 인지 먼저 본다 — 아니면 코멘트도 넣지 않는다. 엔진 호출은 저장소 트랜잭션 밖에서 하고 잠금 하나로 직렬화한다. 엔진이 연결되지 않았으면 `precondition_failed` 이고 결재는 `pending` 으로 남는다. `revise` 의 코멘트를 넣은 뒤 `engine.approve` 가 거부하면 코멘트는 런에 남고 `engine_error` 로 답하며 결재는 `pending` 이다 — 다시 결정하면 코멘트가 한 번 더 들어간다. 순서를 뒤집으면 수정 요청이 코멘트 없이 승인되므로 이대로 둔다 (잠정, S-5).
- 밖에서 풀렸을 때: 대시보드가 `/runs/{id}/approve` · `cancel` 을 부르면 그 런의 `pending` 결재를 `cancelled` 로 닫는다(`server.py` 817 · 836 의 `_released`). 서버를 거치지 않은 변화에 대비해, 결정할 때 런이 더는 `awaiting_approval` 이 아니면 결재를 `cancelled` 로 닫고 409 `stale_state` 를 돌려준다 — 본문의 `inbox` 에 닫힌 결재를 싣는다 (§6.3). `GET /studio/inbox` 도 목록을 읽기 전에 `pending` 인 작업 없는 결재의 런 상태를 보고 같은 정리를 한다(`Inbox.sweep_runs`). 런이 없어졌어도 닫는다. 결정이 진행 중이면 이 정리는 건너뛴다 — 승인 직후 런이 `pending` 인 틈에 결재를 닫지 않게 한다. 엔진이 연결되지 않았으면 하지 않는다 (잠정, S-5).
- 시작할 때: 서버가 뜰 때 수신기가 듣지 못한 정지를 한 번 채운다 (`studio/runs.py` 의 `file_waiting`). 대상은 `awaiting_approval` 이고 `tasks.run_id` 에 없으며 `pending` 인 작업 없는 결재가 없는 런이다 — CLI 가 세운 런과 studio 저장소가 닫혀 있을 때 멈춘 런이 여기에 든다. 결재는 위 "대응"대로 만들고, `next_stage` · `spent_usd` · `limit_usd` 는 이벤트 기록에서 되찾는다: `StopNote.stage` 단계의 마지막 정지 이벤트(`StopNote.kind` 가 `budget` 이면 `budget_exceeded`, `stage_budget` 이면 `stage_budget_exceeded`, `awaiting_approval` 이면 `awaiting_approval`)의 `detail` 이다. 그 이벤트가 없으면 `spent_usd` 는 런의 누적 비용, `limit_usd` 는 0, `next_stage` 는 `StopNote.remaining_stages` 의 첫 단계다. 이미 `pending` 결재가 있는 런은 내용이 달라도 건드리지 않는다 — 시작할 때마다 결재가 닫히고 새로 생기지 않게 한다. 작업 런은 건너뛴다: 그 처리는 §4.8 의 걸음 1이다 (S-8). `StopNote` 가 없는 런도 건너뛴다. 런 하나가 실패하면 기록하고 다음 런으로 가며, 전체가 실패해도 서버 시작을 막지 않는다. 부르는 자리는 `server.py` 맨 아래의 두 번째 startup 함수 `file_waiting_runs` 다 — startup 함수는 등록한 순서대로 돌므로 `recover` 의 `open_store()` 뒤다. 서버가 도는 동안 CLI 가 세운 런은 다음 시작 전까지 결재함에 없다 (잠정, S-5).

**(다) studio 밖에서 작업 런을 조작.** 런이 `tasks.run_id` 에 있으면(작업이 `working` · `blocked` · `review` · `done`) 기존 엔드포인트가 이렇게 답한다.

| 기존 엔드포인트 (`server.py`) | 작업 런일 때 |
|---|---|
| `/runs/{id}/approve` (814) · `/resume` (822) | 409. `{detail: "스튜디오 작업의 런이다. 결재함에서 결정하라"}` |
| `/runs/{id}/changes/apply` · `discard` · `rollback` (839–854) | 409. 같은 안내. 변경의 반영 · 되돌리기는 T7 · T9 · T11 만 한다 |
| `/runs/{id}/cancel` (833) | 받는다. §4.7 표의 `cancelled` 행이 처리한다 |
| `/runs/{id}/interject` (293) | 받는다 |

막는 이유: 밖에서 승인하면 결재는 `pending` 인데 런이 가 버리고, 밖에서 변경을 되돌리면 통과한 작업의 커밋과 폴더가 어긋난다. 오류 본문은 기존 형식(`{detail}`, `_conflict` 214–218)을 따른다. 막는 곳은 `server.py` 맨 아래의 `_outside` 다 — 런이 없으면 먼저 404, 작업 런이면 409. 작업 런인지는 `studio_api.is_task_run` 이 `tasks.run_id` 로 본다. studio 저장소가 열리지 않았거나 읽지 못하면 작업 런이 아닌 것으로 보고 기존대로 받는다 — 대시보드가 studio 레이어 때문에 멈추지 않는다 (잠정, S-5).

CLI 는 서버를 거치지 않고 엔진을 직접 부른다 (S-5 에서 확인): `relay cancel` · `apply` · `discard` · `rollback` · `approve` · `resume` 이 자기 프로세스의 엔진 메서드를 그대로 부른다(`cli.py` 658–672). 그래서 CLI 로 작업 런을 승인하거나 변경을 되돌리는 것은 막지 못한다. 서버가 도는 동안 그 어긋남을 알아채 시스템 방에 알리는 코드는 없다 (미구현. 받을 작업은 V-3 — CAMPAIGN.md 의 V-3 명세에는 직접 없다. 그 작업 계획에서 범위에 넣는다). 있는 것은 둘이다 — `working` 작업의 런이 서버 밖에서 돌다 멈추면 큐가 §4.7 의 표대로 처리하고(`studio/queue.py` `_stopped_elsewhere`), 서버를 띄울 때 §4.8 의 5가 `proposed` · `blocked` · `review` 인데 그 상태를 받치는 `pending` 결재가 없는 작업을 시스템 방에 알린다(`studio/work.py` `_broken`). 작업 없는 런을 CLI 가 풀었으면 (나)의 정리가 그 결재를 닫는다.

### 5.9 알림 — 결재가 아닌 것과 푸시

결재를 만들지 않고 알리기만 하는 것. 모두 시스템 글(`author_kind = 'system'`)이고 `meta.event` 로 구분한다 (§7.6).

| 알림 | 계기 | 글이 가는 방 | `meta.event` |
|---|---|---|---|
| 배치 필요 | 캠페인 자동 진행이 담당 없는 작업을 만남 (§4.10, S-14) | 시스템 방 | `needs_assignment` |
| 선행 반려 | T8 · T9 가 이미 시작된 뒤 작업에 `upstream_rejected` 를 세움 (§4.3) | 뒤 작업의 작업방 | `upstream_rejected` |
| 선행 거절 | 선행이 `rejected` · `cancelled` 라 뒤 작업이 시작되지 못함 (§4.6) | 시스템 방 | `upstream_rejected` |
| 런 생성 실패 | T3 에서 런을 만들지 못함 (§4.3) | 시스템 방 | `run_error` |
| 턴을 넣지 못함 | 채팅 · 읽기 턴을 넣지 못함 (§9.5 의 덧붙임). 직원의 멘션이 §9.5 의 5 (가)에 걸린 경우는 글을 남기지 않고 도구 오류로 답한다 | 계기가 된 그 방 (시스템 방이 아니다) | `run_error` |
| 불변식 깨짐 | 재시작 복구의 5 (§4.8), 밖에서 조작된 작업 런 (§5.8 다) | 시스템 방 | `invariant_broken` |
| 단체방 왕복 한도 | 단체방 · 파트방에서 직원의 멘션으로 들어온 턴이 한도에 닿아 넣지 않음 (§4.6). 멈춘 방마다 한 번이고 즉시 푸시한다 (음소거한 방이어도 간다 — 다른 시스템 알림과 같다) | 그 단체방 | `group_exchange_limit` |
| 일일 보고 | 하루 1회 (S-13) | 시스템 방 (인사 직원 이름으로) | `daily_report` |

"배치 필요"를 결재로 두지 않은 이유: CAMPAIGN.md 가 "알림"이라 적었고, `backlog` 작업은 차단 결재를 가질 수 없다 (F2). 같은 작업에는 담당이 바뀌거나 캠페인을 다시 시작할 때까지 한 번만 알린다 (S-14 확정): 글을 쓸 때 그 작업의 가장 새 `assigned` 이벤트(`task_events`)의 id 를 `meta.assigned_event_id` 에 적는다 (그런 이벤트가 없으면 0. §7.6). 시스템 방에 그 작업의 `needs_assignment` 글이 이미 있으면 둘 중 하나일 때만 다시 쓴다 — 지금의 그 id 가 가장 새 글에 적힌 값보다 크다(글 뒤에 담당이 바뀌었다. 키가 없는 옛 글은 0 으로 본다), 또는 캠페인이 `running` 이 된 직후의 훑기다(시작 · 재개. 전이가 `Effects.after` 에 `campaign` 대신 `campaign_started` 를 넘기고, 그 훑기가 `again` 으로 돈다 — `tasks.CAMPAIGN_STARTED`). "선행 거절"은 (작업, 선행) 쌍마다 한 번이고 캠페인이 `running` 이 된 직후의 훑기에서만 다시 쓴다. 캠페인의 제목 · 목표 · 방식을 고친 뒤의 훑기는 다시 알리지 않는다. 따로 적어 두는 열은 없다 — 시스템 방의 글이 기록이어서 서버를 다시 띄워도 유지된다 (§4.8 의 5 와 같은 방식). 시각은 비교하지 않는다 — 이벤트 id 는 단조 증가라 글을 쓴 그 초 안의 변화도 본다 (남는 틈은 §4.10).

"선행 반려"의 글은 T8 · T9 의 결정 뒤에 쓴다 (`Inbox._upstream`): 표시가 선 뒤 작업마다 그 작업방에 한 줄, `meta` 는 `{event: upstream_rejected, task_id: 뒤 작업, upstream_task_id: 반려된 선행}` 이다. 런에 넣는 결정문이 아니다 — 방의 글일 뿐이고 턴을 만들지 않는다.

푸시 (N-1 · N-2 · N-3). 푸시에는 제목 · 한 줄 · 딥링크 · 알림 액션만 싣는다.

| 계기 | 등급 | 딥링크 `{type, id}` |
|---|---|---|
| `inbox.created` (`hire_recommendation` 을 뺀 9종) | 즉시 | `inbox` · 결재 id |
| `inbox.created` (`hire_recommendation`) | 조용히. 재알림 대상이 아니다 | `inbox` · 결재 id |
| 위 표의 알림 중 일일 보고를 뺀 것 (`message.created`, `author_kind = 'system'`, `meta.event` 가 일곱 중 하나: `needs_assignment` · `upstream_rejected` · `run_error` · `invariant_broken` · `group_exchange_limit` · `disk_low` · `usage_high`) | 즉시 | `task` · 작업 id (`meta.task_id` 가 있을 때) 또는 `room` · 글이 쓰인 방 id |
| 프로젝트 생성이 끝남 · 실패함 (M-8, §7.4 `job.finished`) | 즉시 | `project` · 프로젝트 id (`status` 가 `done` 일 때) 또는 `job` · job id |
| 사용량 한도 | 즉시. 계기는 `meta.event = usage_high` 인 시스템 글이다 (V-4 확정 — 아래 목록) | `room` · `room_system` |
| 직원이 쓴 글 (`message.created`, `author_kind = 'employee'`) | 조용히. `rooms.muted` 인 방은 보내지 않는다 | `room` · 방 id |
| 작업의 진행 — 시작(`task.transitioned` 의 T3)과 `task.updated` 의 `progress` | 조용히 | `task` · 작업 id |
| 일일 보고 | 묶음 | `room` · `room_system` |
| 엔진 알림 가운데 정지가 아닌 것 — `campaign_done` · `campaign_paused`, 그리고 작업 런도 채팅 · 읽기 턴의 런도 아닌 런의 `run_failed` (N-1 이 더한 행) | 즉시 | `task` · 작업 id (그 런이 작업의 런일 때. 아니면 없다) |

- 일일 보고 (S-13 확정, `studio/stats.py`). 서버가 `Config.studio_report_at`(`HH:MM`, 서버 현지 시각. 기본 `18:00`, 빈 값이면 쓰지 않는다)이 지난 뒤의 첫 확인에서 하루 한 번 시스템 방에 쓴다 — 서버 시작 때와 그 뒤 1분마다 본다(`Reporter.watch`. `server.py` 에는 타이머 연결만 있다). 글쓴이는 재직 중인 인사 직원이고, 없으면 system 글이다 — 푸시는 글쓴이가 아니라 `meta.event` 로 가린다. 본문은 정확히 세 줄 `한 일: …` / `막힌 것: …` / `결재 대기: …` 다. 재료는 지난 보고부터 지금까지(첫 보고는 24시간) `done` 이 된 작업, 지금 `blocked` 인 작업, 지금 대기 중인 결재의 제목이다. 문장은 `Config.studio_report_model`(기본 `haiku`, 빈 값이면 부르지 않는다)을 1회 불러 다듬고, 호출이 실패하거나 답이 세 줄 형식(줄마다 200자 이내)을 어기면 규칙으로 만든 세 줄(개수와 제목 3개까지, 없으면 `없음`)을 쓴다. `meta` 는 `{event: daily_report, date, from, to}` 다 (§7.6). `meta.date` 는 `meta.to` 의 날짜, 곧 보고를 시작한 시각의 날짜다 — 모델 호출이 자정을 넘겨도 그 글은 시작한 날의 보고이고, 다음 날의 보고는 따로 쓴다. 같은 날(`meta.date`, 서버 현지 날짜)의 글이 시스템 방에 이미 있으면 쓰지 않는다 — 글이 기록이라 서버를 다시 시작해도 유지된다. 이 모델 호출의 사용량은 usage DB 에 적지 않는다.
- 디스크 경고 (V-3 확정, `studio/disk.py`). 서버가 `machine.disk(Config.runs_dir)` 로 읽은 여유가 `Config.studio_disk_min_free_gb`(기본 `5.0` GB, 0 이하면 경고가 없다) 아래이면 시스템 방에 system 글 `meta.event = disk_low` 를 쓰고 즉시 푸시한다(`push.NOTICE_EVENTS`. 딥링크는 `room` · `room_system`). 서버 시작 때와 그 뒤 10분마다 본다(`DiskWatch.watch` · `CHECK_EVERY = 600`. `Reporter.watch` 와 같은 모양이고 `server.py` 에는 타이머 연결만 있다). 임계는 볼 때마다 읽는다. 같은 부족 상태에서는 한 번만 알린다 — 시스템 방의 가장 새 디스크 글(`disk_low` · `disk_ok`)이 `disk_low` 이면 쓰지 않는다. 글이 기록이라 서버를 다시 띄워도 되풀이하지 않는다. 여유가 임계 이상으로 돌아오면 `disk_ok` 글을 쓰고(푸시하지 않는다 — 급하지 않다) 그 뒤 다시 모자라면 `disk_low` 를 다시 쓴다. 디스크를 읽지 못했으면(`free_bytes` 가 없다) 아무것도 쓰지 않는다. `meta` 는 `{event, free_bytes, min_free_gb, path}` 다 (§7.6).
- 사용량 한도 (V-4 확정, `studio/usage.py`). `UsageWatch` 가 1분마다(`CHECK_EVERY = 60`. `DiskWatch` 와 같은 타이머 모양, `server.py` 에는 연결만 있다) `providers.status('claude')` 의 `limits` 를 본다. `gating` 이고 `stale` 이 아닌 창의 `utilization` 이 `Config.studio_usage_warn_at`(기본 `0.90`, 0 이하면 끔, 볼 때마다 읽는다) 이상이면 시스템 방에 system 글 `meta.event = usage_high` 를 쓰고 즉시 푸시한다(`push.NOTICE_EVENTS` 의 '사용량 한도'. 딥링크는 `room` · `room_system`). 창마다 한 번만 알린다 — 키는 (provider, window, `resets_at`)이고, 시스템 방의 `usage_high` 글이 기록이라 서버를 다시 띄워도 되풀이하지 않는다. 창이 초기화되면 `resets_at` 이 바뀌어 다음 창은 새로 알린다. 기본 동작은 알림뿐이다 — 작업은 그대로 시작한다. `Config.studio_usage_hold` 를 켠 경우의 보류와 결재는 §4.6 이다. `providers.py` 의 95% 전환은 이 알림과 따로 그대로 작동한다. `meta` 는 `{event, provider, window, utilization, resets_at, warn_at, held}` 다 (§7.6).
- 등급 (N-2 확정). 서버는 등급을 FCM 메시지의 세 자리에 싣는다: `data.grade`(`urgent` · `quiet` · `digest`), `android.priority`(즉시만 `HIGH`, 나머지는 `NORMAL`), `android.notification.channel_id`(`katae_urgent` · `katae_quiet` · `katae_digest`). 묶음은 `android.notification.tag` 가 `katae_digest` 하나라 서랍에서 한 건으로 겹친다. 소리 · 진동은 앱이 그 채널 셋을 만들 때 정한다 (A-11) — 백그라운드에서는 시스템이 채널로, 포그라운드에서는 앱이 `data.grade` 로 가린다. `data` 는 딥링크가 없어도 `grade` 를 싣는다.
- 계기 (N-2 확정). 엔진 알림을 뺀 표의 행은 모두 스트림 이벤트라, 결재와 같은 커밋 수신기에서 이벤트를 분류해(`push.classify`) 보낸다 — 글과 이벤트를 쓰는 자리는 고치지 않았다. 직원 글의 제목은 직원 이름(없으면 방 제목)이고 한 줄은 글의 첫 줄 80자다. 시스템 알림의 제목은 알림 이름, 작업 진행의 제목은 작업 제목이고 한 줄은 "작업 시작" 또는 진행 메모의 첫 줄이다. owner 가 쓴 글과 그 밖의 시스템 글(결재 · 결정을 알리는 글 등)은 보내지 않는다. `job.finished` 이벤트는 `studio/jobs.py`(M-8)가, `daily_report` 글은 `studio/stats.py`(S-13)가 쓴다. `job.finished` 의 `status` 가 `cleaned` 면 보내지 않는다 — owner 가 시킨 정리다. `done` 이 아닌 그 밖의 값은 실패로 보낸다 (N-2 마무리). 채팅 · 읽기 턴이 실패하면 방의 `run_error` 글만 푸시가 된다 (N-2 마무리) — 한 사건에 한 번이고, 그 푸시는 방을 연다. 엔진의 `run_failed` 는 알림이 온 순간 그 런이 추적 중인 턴의 런이면(`api.tracked_run`) 발송기가 줄에 넣지 않는다: 추적은 턴이 `advance` 하는 동안만 있고 엔진의 알림은 그 안에서 동기로 온다. 서버 재시작으로 끊긴 턴의 런은 가리지 않는다 — 추적도 `run_error` 글도 없어 푸시는 많아야 `run_failed` 한 건이다(딥링크 없음).
- 방해 금지 (N-2 확정). 기기마다 `devices.settings_json` 의 `quiet_hours` = `{start, end, utc_offset_min}` 로 둔다 (§3.14). `start` · `end` 는 그 기기 벽시계의 `HH:MM` 이고 `start` 는 포함, `end` 는 제외다. `start` 가 `end` 보다 늦으면 자정을 넘기고, 둘이 같으면 방해 금지가 없다. `utc_offset_min` 은 그 시계가 UTC 보다 앞선 분이고, 없으면 서버 PC 의 현지 시각으로 본다 — IANA 시간대 이름은 받지 않는다(Windows 에는 tzdata 가 없을 수 있다). 서머타임이 있는 지역이나 여행 중에는 앱이 오프셋을 다시 보낸다. 방해 금지인 기기에는 즉시 등급을 버리거나 미루지 않고 조용히로 낮춰 보낸다 — 버린 푸시는 되살릴 수 없다. 조용히 · 묶음은 그대로 간다.
- 음소거 (N-2 확정). `rooms.muted` 는 그 방의 직원 글 푸시에만 걸린다. 그 방에 쓰인 시스템 알림, 작업 진행, 결재 푸시에는 걸리지 않는다. `PATCH /studio/rooms/{id}` 가 바꾼다 (§6.4).
- 재알림 (N-2 확정). `pending` 인 결재는 `reminded_at`(없으면 `created_at`)에서 `Config.studio_remind_after` 초(기본 7200 = 2시간. 0 이하면 재알림이 없다)가 지나면 즉시 등급으로 다시 보낸다 — 제목 · 딥링크 · `data.action` 은 같고 한 줄은 종류 이름 뒤에 " · 다시 알림"이 붙는다. 서버가 시작할 때 한 번, 그 뒤 60초마다 훑는다(`Pusher.watch`. 설정값은 훑을 때마다 읽는다). 기준 시각이 DB 에 있어 서버를 다시 띄워도 이어진다. 보내 본 기기가 하나라도 있으면 FCM 의 답과 무관하게 `reminded_at` 을 적는다(스트림 이벤트는 내지 않는다) — 적지 않으면 남아 있는 실패를 1분마다 다시 보낸다. 방해 금지인 기기는 건너뛰고, 모든 기기가 방해 금지면 적지 않는다 — 방해 금지가 끝난 뒤 첫 훑기에 울린다. 일부 기기만 방해 금지면 `reminded_at` 이 적히므로, 건너뛴 기기는 방해 금지가 끝난 직후가 아니라 다음 간격 뒤의 재알림에 울린다 — 그 기기도 만들어질 때의 푸시는 이미 받았고, 기기별 재알림 시각은 두지 않는다 (N-2 마무리). 방해 금지 중에 만들어진 결재는 그때 조용히로 한 번 가고, 다시 울리는 것은 만들어진 때부터 간격이 지난 뒤다. 방 음소거는 결재 푸시에 걸리지 않는다. 채용 권장은 재알림하지 않는다 — 재알림이 즉시 등급이라 조용한 등급이 한 번 뒤에 깨진다 (`push._remind` 가 건너뛴다).
- 알림 액션 (N-3 확정). 앱이 알림 버튼을 추측 없이 만들도록 `data` 에 문자열 키 `action` 을 싣는다 (FCM `data` 는 문자열만 된다). 딥링크 `{type, id}` 와 `grade` 는 그대로다. 결재 푸시: `action` = `approve`(종류가 `inbox.NOTIFY_OK` — `task_proposal` · `hire` · `design_approval` · `pre_approval` · `budget`: 알림에서 승인하고 열 수 있다) 또는 `open`(`risky_command` · `final_review` · `structure_choice` · `question` · `hire_recommendation`: 알림에서 결정할 수 없다, 열기만). 직원이 쓴 글의 푸시: `action` = `reply` 와 `room_id`(답장할 방 id). 그 밖의 푸시(일일 보고 · 시스템 알림 · 작업 진행 · 프로젝트 · 엔진 알림)에는 `action` 이 없다 — 버튼 없이 딥링크만이다. 모든 값은 문자열이고, 재알림도 최초 푸시와 같은 값을 싣는다(`push.inbox_link` 하나가 만든다). 방해 금지로 등급이 낮아져도 `action` 은 그대로다 — 등급은 소리만 정한다. 새 경로는 없다: 앱은 기기 토큰(`sdt_`)으로 승인은 `POST /studio/inbox/{id}/decide` 에 `{decision: approve, via: notification}` 으로, 답장은 `POST /studio/rooms/{room_id}/messages` 에 `{body, client_msg_id}` 로 보낸다 (§6.4). `via = notification` 의 코멘트 · `data` · 그 밖의 결정과 `open` 종류는 `invalid_decision`(§5.2, `inbox.py`의 기존 규칙)이고, 이미 처리된 결재는 `already_decided`(409)다 — 앱은 409 면 결재를 연다.
- 기존 토스트(`Notifier`, `notify.py` 45–67)는 그대로 두고 채널을 더한다 (N-1). 연결 자리는 §11.3.
- N-1 이 붙인 것(`studio/push.py`)은 표의 첫 행과 마지막 행이다. 결재는 `inbox.created` 를 쓴 커밋을 듣고(`api.on_commit`) 보낸다 — 제목은 결재의 `title`, 한 줄은 종류 이름(`inbox.KIND_LABEL`)이고, 보낼 차례가 왔을 때 그 결재가 이미 `pending` 이 아니면 보내지 않는다. 엔진 알림 셋은 `Fanout` 의 수신기로 듣는다 — 제목은 `NOTIFY_KINDS` 의 아이콘과 라벨, 한 줄은 토스트 제목의 첫 줄 80자다. 정지 셋(`awaiting_approval` · `budget_exceeded` · `stage_budget_exceeded`)은 그것이 된 결재의 푸시로만 간다 — 한 사건에 한 번. 표의 나머지 행과 등급 · 방해 금지 · 음소거 · 재알림은 N-2 가 붙였다 (위). `run_done` 은 보내지 않는다 — 런의 끝은 검수 결재나 직원의 글로 이미 간다. 작업 런의 `run_failed` 도 보내지 않는다 (N-2 확정): 그 실패는 결재(`question` · `run_failed`)가 되어 그 푸시로 가거나, 자동으로 다시 줄에 선다 (`studio/work.py`).
- 받는 기기는 폐기되지 않았고 `fcm_token` 이 있는 기기 모두다 (§3.14). FCM 의 오류 본문에 `errorCode` 가 `UNREGISTERED` 일 때만(FCM 은 보통 404 로 준다) 그 기기의 `fcm_token` 을 NULL 로 바꾼다 — 그 사이 다시 페어링해 값이 달라졌으면 두고, 기기를 폐기하지는 않는다. `errorCode` 없는 404 는 그 밖의 4xx 처럼 보내지 못한 것으로 끝내고 토큰을 둔다 (N-1 마무리) — 프로젝트나 주소가 틀려도 404 이고, 잘못 비우면 모든 기기를 다시 페어링해야 하지만 못 비우면 푸시마다 요청 한 번이 헛될 뿐이다. 죽은 토큰에 FCM 이 실제로 `UNREGISTERED` 를 싣는지는 실기기로 확인하지 않았다. 429 · 5xx · 네트워크 오류 · 401(액세스 토큰을 새로 받는다)은 1초 · 3초 뒤에 다시 해 모두 세 번 한다(`Retry-After` 가 있으면 그 값, 30초까지). 액세스 토큰을 받는 요청도 같은 규칙으로 다시 하고, 끝내 받지 못하면 60초 동안 다시 묻지 않는다 (N-1 마무리) — 그 사이의 푸시는 요청 없이 버린다. 기기마다 · 일감마다 다시 물으면 하나뿐인 워커가 묶여 뒤 푸시가 밀리기 때문이다. 그 밖의 4xx 는 보내지 못한 것으로 끝내고 토큰은 둔다 — 400 은 메시지 탓일 수 있다. 보내지 못한 푸시는 버린다: 결재는 결재함에 남아 있다. 발송은 워커 스레드 하나가 줄(1000개까지. 넘치면 버린다)에서 차례로 해서, 커밋한 요청은 Google 을 기다리지 않는다.
- 키는 `Config.studio_fcm_key`(`relay.config.local.yaml`)에 적은 서비스 계정 키 파일이다. 기본은 빈 값 = 푸시 없음이고, 적지 않은 자리는 찾아보지 않는다. 첫 푸시 때 읽는다 — 파일이 없거나 읽지 못하거나 서명할 수 없으면 경고 한 줄을 남기고 꺼진 채 서버는 그대로 돈다. 경로를 바꾸면 다음 푸시 때 다시 읽고, 같은 경로의 파일을 바꾸거나 나중에 놓았으면 서버를 다시 띄운다. 파일은 저장소 밖 `%USERPROFILE%\.katae\` 에 둔다 — 가드가 직원의 읽기를 막는 자리다 (§10). 다른 자리에 적어도 그 경로가 단계마다 `guard_ctx.json` 의 `secrets[]` 에 더해져(`studio/guard.py` `write_ctx`, N-1 마무리) 그 파일을 가리킨 읽기는 막힌다. 다만 파일 하나로 더해진 비밀이라 그 폴더를 재귀로 읽는 것(`grep -r` 등)은 막지 않는다 — `studio_guard.r_secret_access` 의 재귀 검사는 `.json` 같은 파일 경로를 빼고 폴더만 본다. 그래서 프로젝트 폴더 안에는 두지 않는다. `httpx` · `pyjwt` · `cryptography` 는 `pyproject.toml` 에 적지 않았다 — `mcp` 가 끌어오고, 없으면 발송기만 꺼진다.

### 5.10 CAMPAIGN.md 와의 대조

CAMPAIGN.md §2(76행), S-5, A-9 가 적은 결재는 모두 9가지이고 이름도 같다. 개수는 어긋나지 않는다. 다르게 정한 것은 아래이고 §12 가 모은다. `[§12]`

| 번호 | CAMPAIGN.md | 이 문서 |
|---|---|---|
| 1 | A-9: 버튼은 "승인 / 반려 / 수정 요청" | 종류마다 결정 값이 다르다 (§5.2 의 대응표). 셋이 다 있는 것은 `hire` · `pre_approval` · `final_review` 뿐이다 |
| 2 | H-6: 인사가 "정책 또는 지침 수정안을 결재로" | 10번째 종류를 두지 않고 `hire` 의 `action` 으로 받는다 (§5.3) |
| 3 | M-7 "무응답이면 결재로", S-9 "한도 초과 시 `ask_owner` 결재" | `question` 의 `reason` 으로 받는다 (§5.3) |
| 4 | S-14: 담당 미정이면 "배치 필요로 알림" | 결재가 아니라 시스템 글과 푸시다 (§5.9) |
| 5 | S-5: "결정에는 코멘트와 수정 요청을 실을 수 있다" | 수정 요청은 `revise` · `rework` 결정 값이고, `task_proposal` 의 수정은 `approve` 의 `edits` 다 |
| 6 | S-5: 기존 런의 승인 대기 · 예산 초과도 결재로 | 작업 런은 §4.7, 작업 없는 런은 `task_id` 없는 결재 (§5.8). 작업 런은 기존 `/runs` 승인 통로를 막는다 |
| 7 | 예산 결재의 반려가 무엇을 하는지 적지 않았다 | 런을 끊지 않고 "검수 요청을 올려라"로 잇는다 (§5.5) |
| 8 | S-12: 검수 요청 시 패키지 "생성" | 직원이 낸 것만 저장하고 나머지는 읽을 때 계산한다 (§5.4) |
| 9 | N-3: 위험 명령 · 최종 검수는 알림에서 승인 불가 | 그 둘에 더해 `structure_choice` · `question` 도 알림에서 결정할 수 없다 (고를 것이나 답이 필요하다) |

## 6. REST

근거: S-3 · S-8 · S-12 · S-13, M-1 · M-2 · M-7 · M-8, H-3. 기존 서버의 접점은 §11.3.

### 6.1 공통 규칙

- 모든 경로는 `/studio` 로 시작하고 JSON 을 주고받는다. 성공은 200, 만들면 201, 오래 걸리는 일을 맡기면 202 다.
- 응답의 행은 §3 의 컬럼 그대로다. `_json` 컬럼은 접미사를 뗀 키로, 불리언은 `true` · `false` 로 준다 (§3.1). §6.4 의 표에는 그 밖에 덧붙이는 필드만 적는다.
- 목록은 `{items: [...]}` 다. 커서가 있는 목록(글 · 작업 이력 · 처리된 결재)은 `has_more` 를 더한다. 목록의 `?archived=1` · `?retired=1` 은 보관 · 퇴사한 행도 함께 준다는 뜻이다 — 그것만 주는 것이 아니다 (S-3).
- 시각은 서버의 지역 시각이고 시간대 표시가 없다 (§3.1). `GET /studio/machine` 이 `now` 와 `utc_offset_minutes` 를 주고, 여러 컴퓨터를 보는 앱이 그 값으로 바꾼다. `[§12]`
- 열거 값과 id 접두사는 §1.5 · §3.1 이다.
- OpenAPI 는 `design/studio/openapi.json` 으로 내보낸다. 명령은 `uv run relay studio openapi` 이고, `--check` 를 붙이면 쓰지 않고 저장된 파일과 비교만 한다 (S-3). 파일에는 `/studio/*` 경로만 있다. 요청 본문 · 쿼리 · 경로가 계약이고, 응답은 타입 없이 객체로만 적힌다 (잠정, S-3). 저장된 파일이 지금 코드와 같은지는 테스트가 본다.

### 6.2 인증

`/studio/*` 의 HTTP 요청은 이 순서로 본다. WS 는 §7.1.

1. 출처 검사와 네트워크 검사는 기존 가드 그대로다 (`server.py` 96–101). 걸리면 403 이고 본문은 기존대로 `{detail}` 이다.
2. `POST /studio/devices/pair` 는 토큰 없이 받는다. 폰은 교환 전에 토큰이 없다. 코드를 내주는 `POST /studio/devices/pairing` 은 예외가 아니다 — 3 · 4 를 그대로 탄다 (§6.5 의 1).
3. `Authorization: Bearer <토큰>` 이 있으면 토큰으로만 판정한다. 루프백이어도 그렇다. 맞는 토큰이 아니면 401 이다.
4. 헤더가 없으면 401 이다. 루프백이어도 그렇다 — `/studio/*` 는 루프백 요청에도 토큰을 요구한다 (§12.6 의 5). `/studio` 가 아닌 기존 경로는 지금처럼 루프백을 토큰 없이 통과시킨다 (기존 가드 107행). 직원이 스크립트로 서버를 불러 결재를 스스로 승인하는 길을 막으려는 것이다.

401 의 `code` 는 토큰의 접두사가 아니라 경로로 정한다. **행위자 경로**(`/studio/mcp/*` · `/studio/guard/*`)는 `actor_invalid`, 그 밖의 `/studio/*` 는 `unauthorized` 다. 틀린 토큰은 어떤 종류인지 믿을 수 없어서다 — 경로로 가르면 클라이언트마다 401 코드를 하나만 알면 된다. 헤더 없는 요청도 같다(4) — 루프백이든 아니든 행위자 경로에서는 401 `actor_invalid`, 그 밖에서는 401 `unauthorized` 다. 403 `actor_not_allowed` 는 맞는 owner 토큰이 행위자 경로에 올 때 쓴다 (아래).

| 토큰 | 모양 | 주체 | 판정 |
|---|---|---|---|
| 공용 | `Config.auth_token` (`config.py` 46) | owner | `hmac.compare_digest` (`server.py` 111) |
| 기기 | `sdt_` + `secrets.token_urlsafe(32)` | owner | SHA-256 hex 가 `devices.token_hash` 에 있고 `revoked_at` 이 NULL 이다. 맞으면 `last_seen_at` 을 갱신한다 (1분에 한 번이면 된다) |
| 행위자 | `sat1.<claims>.<서명>` (§8.2) | employee | §8.2 |

- 주체별 경로: 행위자 경로(`/studio/mcp/*` · `/studio/guard/*`)는 행위자 토큰만, 나머지 `/studio/*` 는 owner 만 받는다. 403 `actor_not_allowed` 는 주체가 정해졌는데 그 경로의 주체가 아닐 때만 쓴다 — owner(맞는 공용 · 기기 토큰)가 행위자 경로에 오거나, §8.2 의 검사 1–3 을 지난 행위자 토큰이 owner 경로에 올 때다. 주체를 정하지 못하면 401 이다: 헤더가 없는 요청과 끝난 런의 행위자 토큰은 owner 경로에서 401 `unauthorized`, 행위자 경로에서 401 `actor_invalid` 다.
- 기존 가드와 다른 점은 둘이다. `[§12]` (1) 공용 토큰을 설정하지 않은 서버에 원격으로 오면 기존 가드는 403 인데(109–110) `/studio/*` 는 401 이다 — S-3 · M-2 의 완료 기준이 401 이다. 기기 토큰은 공용 토큰이 없어도 받는다 (M-2 가 확정했다). (2) 루프백도 토큰을 요구한다 — 헤더가 없으면 401 이고(4), 있으면 검사한다(3). 기존 가드는 루프백이면 토큰을 보지 않는다(107). 직원의 스크립트도, 끝난 런의 행위자 토큰을 든 MCP 서버도 루프백이라는 이유로 owner 가 되지 않게 한다.
- 구현 자리: 가드의 토큰 검사(107–112)가 경로가 `/studio/` 로 시작하면 studio 인증 함수로 넘긴다. 이 갈래는 루프백 통과(107)보다 앞에 둔다 — 뒤에 두면 헤더 없는 루프백 요청이 studio 인증에 닿지 않는다. S-3 의 "기존 토큰 가드를 그대로 탄다"와 달리 가드에 갈래가 하나 생긴다. `[§12]`
- 기기 토큰은 `/studio/*` 에서만 통한다. 폰을 잃었을 때 그 토큰으로 `/admin/restart` · `/repos` 같은 기존 경로를 부를 수 없게 한다. 그래서 앱이 읽는 기존 조회는 `/studio` 아래에 별칭을 둔다 (§6.4 의 런 · 사용량). `[§12]`
- 대시보드의 studio 화면이 토큰을 묻는 방법. 페이지 `/` 와 `/studio` 가 아닌 기존 경로는 루프백이면 지금처럼 토큰 없이 열린다. 대시보드가 `/studio/*` 를 처음 불러 401 을 받으면 기존 토큰 창(`dashboard.html` 462–486 의 `askToken`)으로 공용 토큰을 묻고, `localStorage` 의 `katae-token` 에 저장해 그 뒤 요청에 `Authorization: Bearer` 로 붙인다 — 원격 접속 때의 흐름(487–500) 그대로이고 안내 문구만 studio 용으로 바꾼다. 저장된 토큰이 맞으면 묻지 않는다. WS 는 같은 토큰을 첫 메시지 `auth` 로 보내고(§7.1) 4401 로 끊기면 다시 묻는다. 토큰 값은 서버 PC 에서 `uv run relay remote status` 로 본다 (대시보드의 기존 안내 문구 473 과 `cli.py` 256. 출력 모양은 확인 필요, S-3). 공용 토큰을 설정하지 않은 서버에서는 studio 화면을 열 수 없다 — 401 의 `detail` 이 설정 방법(`relay.config.local.yaml` 의 `auth_token` 또는 환경 변수 `KATAE_TOKEN`)을 알리고, 서버가 토큰을 대신 만들지 않는다 (잠정, S-3). 서버가 맡는 것은 401 과 그 `detail` 까지다. 대시보드 쪽 변경은 studio 화면을 붙이는 작업이 이 흐름대로 한다.
- 남는 구멍: (가) `/studio` 가 아닌 기존 경로(`/admin/restart` · `/runs/{id}/approve` 등)는 루프백을 그대로 믿으므로 직원의 셸과 스크립트가 토큰 없이 부를 수 있다. (나) 공용 토큰을 읽어 낸 스크립트는 owner 로 `/studio/*` 를 부를 수 있다. 셸의 요청은 §10.3 의 `server_control` 이 받고, 스크립트 안의 요청과 토큰 읽기는 남는다 (§10.8 의 3). owner 확정 (§12.6 의 5): `/studio/*` 는 루프백 요청에도 토큰을 요구한다(4). 기존 경로의 루프백 신뢰는 그대로 둔다.

### 6.3 오류

오류 본문은 `{detail, code}` 다. `detail` 은 사람이 읽는 한국어 문장이고(기존 오류와 같은 키, `server.py` 214–218), `code` 는 아래 값이다. 덧붙는 필드는 표에 적었다.

| `code` | HTTP | 뜻 | 덧붙는 필드 |
|---|---|---|---|
| `unauthorized` | 401 | 행위자 경로 밖의 `/studio/*` 에서 토큰이 없거나 틀렸거나 폐기됐다 (§6.2) | |
| `actor_invalid` | 401 | 행위자 경로(`/studio/mcp/*` · `/studio/guard/*`)에서 토큰이 없거나 §8.2 의 검사 1–3 에 걸렸다 | |
| `actor_not_allowed` | 403 | 이 주체가 할 수 없다 (§4.5, §6.2) | |
| `not_found` | 404 | 그 id 가 없다 | |
| `illegal_transition` | 409 | §4.5 | `from` · `to` |
| `precondition_failed` | 409 | §4.5. `detail` 에 무엇이 빠졌는지 | `failed[]` |
| `stale_state` | 409 | §4.5. 다시 읽고 다시 요청한다. 작업 없는 결재의 런이 밖에서 풀려 그 결재를 닫았을 때도 이 코드다 (§5.8 나) | `inbox` (§5.8 나일 때만) |
| `already_decided` | 409 | 결재가 `pending` 이 아니다 (§5.1) | `inbox` |
| `conflict` | 409 | 이름 · 포트 · repo 가 겹친다 | `field` |
| `engine_error` | 409 | 엔진이 `ValueError` 로 거부했다 (`_conflict` 와 같은 처리) | |
| `pairing_invalid` | 400 | 페어링 코드가 틀렸거나 만료됐거나 이미 썼다 | |
| `payload_too_large` | 413 | 첨부가 25MB 를 넘는다 (`server.py` 144 · 160 와 같은 한도) | |
| `missing_fields` | 422 | §4.5 | `fields[]` |
| `invalid_decision` | 422 | §5.1 의 3 | `allowed[]` |
| `unavailable` | 503 | studio 레이어가 시작하지 못했다 (§3.2 의 2). 기기 토큰을 DB 에서 확인하지 못했을 때도 같다 (§6.2) | |

- FastAPI 의 요청 검증 오류는 기본 형식(422, `detail` 이 목록)으로 나가고 `code` 가 없다.
- 기존 가드의 403(출처 · 네트워크)도 `code` 가 없다.
- `TransitionError`(§4.5)의 다섯 코드는 위 표의 같은 이름으로 옮긴다.
- 덧붙는 필드의 모양 (잠정): `failed[]` 는 `precondition_failed` 에 항상 있는 배열이고, 원소는 snake_case 문자열 코드다(객체가 아니다). 걸린 것이 여럿이면 모두 넣는다. 코드를 정해 두지 않은 조건이면 빈 배열이고 설명은 `detail` 이 한다. 코드 이름은 각 절이 정한다 — 절 단위 색인은 이 목록 아래의 표다. `fields[]` 는 비어 있는 인자 · 필드 이름의 문자열 배열이다. `from` · `to` 는 지금 상태와 가려던 상태이고 §4.1 의 이름을 쓴다. MCP 결과도 같은 이름과 모양으로 싣는다 (§8.3).

`failed[]` 의 코드를 정하는 절 (절 단위 색인. 코드 이름은 그 절에 있고 여기에 옮겨 적지 않는다):

| 절 | 무엇에 걸렸을 때 | 코드가 나가는 곳 |
|---|---|---|
| §6.6 '직원의 첨부' | 직원이 넘긴 파일 경로 — 없음 · 허용 폴더 밖 · 크기 | §8.4 의 `room_post` · `ask_owner` · `task_request_review` |
| §8.4 `task_request_review` | 하네스 게이트 (§4.3 T6, S-11) | 같은 도구 |
| §9.1 | `MEMORY.md` 의 크기 | 그 파일의 `PUT` |
| §9.4 | `employee.yaml` 의 저장 검사 | `employee.yaml` 을 쓰는 모든 길(§9.6)과 `employee_draft` 의 `problems[]` |
| §9.5 | 런을 만들 때의 단계 이름 · provider · 모델 · 상한 | `start`(§6.4), 그리고 채팅 · 읽기 턴을 넣지 못한 `room_post` |
| §9.6 | 직원 파일의 직접 수정 · 되돌리기 — 읽은 뒤에 파일이 바뀌었다, 이력에 없는 커밋 | `PUT /studio/employees/{id}/files/{name}`, `POST /studio/employees/{id}/revert` |
| §6.4 (직원 메모리) | 메모리 항목 — 그런 항목이 없다, 고친 뒤 MEMORY.md 가 4096바이트를 넘는다 | `PATCH` · `DELETE /studio/employees/{id}/memory/{item_id}` |
| §6.4 (직원 경로) | 직원의 배치 · 휴직 · 퇴사 — 퇴사한 직원 · 인사에게 프로젝트 · 없는 프로젝트 · 보관된 프로젝트 · 진행 중인 작업(§4.9) | `POST /studio/employees/{id}/assign` · `leave` · `retire` |
| §4.3 'T5 · T7 · T8 · T9 · T11 의 `failed[]`' | 그 작업의 런과 변경의 상태 — 런이 없다 · 돌고 있다 · 닫히지 않는다, 이미 반영했다 · 이미 되돌렸다, 스냅샷이 없다, 폴더를 다른 런이 쓴다, 다른 작업이 시작됐다 | `POST /studio/inbox/{id}/decide`, `POST /studio/tasks/{id}/cancel` |

`failed[]` 로 검색해 모은 색인이다. 표에 없는 절의 `precondition_failed` 는 코드를 정해 두지 않은 것으로 보고 빈 배열을 낸다. 절이 코드를 새로 정하면 이 표에 그 절을 더한다.

### 6.4 엔드포인트

공통 오류(`unauthorized` · `actor_invalid` · `actor_not_allowed` · `not_found` · 검증 오류)는 "오류" 열에 다시 적지 않는다. "행"은 §3 의 그 테이블 행이다.

**컴퓨터 · 기기**

| 메서드 · 경로 | 요청 | 응답 | 오류 | 작업 |
|---|---|---|---|---|
| `GET /studio/machine` | — | `{name, server_version, schema_version, now, utc_offset_minutes, doctor[], engines[], disk, providers[], editors[], limits}` | | M-1 |
| `GET /studio/machine/limits` | — | `{max_runs, max_exchanges, group_max_exchanges}` | | V-4 |
| `PATCH /studio/machine/limits` | `{max_runs?, max_exchanges?, group_max_exchanges?}` (모두 정수, 하나 이상) | `{max_runs, max_exchanges, group_max_exchanges}` | 422 | V-4 |
| `POST /studio/devices/pairing` (owner 토큰) | — | `{code, expires_at, qr}` | | M-2 |
| `POST /studio/devices/pair` (토큰 없음) | `{code, name, platform, fcm_token}` | 201 `{device, token, machine}` | `pairing_invalid` | M-2 |
| `GET /studio/devices` | — | 기기 행 목록 (`token_hash` 제외) | | M-2 |
| `PATCH /studio/devices/{id}` | `{name, fcm_token, settings}` 중 일부. `id` 에 `me` 를 쓰면 지금 토큰의 기기. 보낸 키만 바꾼다 — `fcm_token` 의 `null` · 빈 문자열은 지우고, 같은 값을 쥔 다른 기기는 그 값을 내놓는다. `settings` 는 통째로 바뀐다 (§3.14) | 기기 행 (`token_hash` 제외) | `not_found` (없거나 폐기된 기기) · `precondition_failed` (공용 토큰으로 `me`) | A-11, N-2 |
| `POST /studio/devices/{id}/revoke` | — | 기기 행 | | M-2 |

- `machine` 의 필드 (M-1 이 정했다. `studio/machine.py`). `name` 은 컴퓨터 이름, `server_version` 은 서버가 올려 둔 코드의 해시 12자(`server.py` 의 `RUNNING_CODE`), `schema_version` 은 `MIGRATIONS` 의 마지막 버전(§3.2), `now` · `utc_offset_minutes` 는 이 컴퓨터의 지역 시각과 UTC 와의 차(분)다 (§6.1). `doctor` 는 `relay doctor` 의 점검 목록 `[{id, ok, what, hint}]` — `ok` 가 `null` 이면 문제로 세지 않는 참고 줄이고, `hint` 는 실패한 점검에만 있다. `engines` 는 `[{version, path, exists}]` — `HKLM\SOFTWARE\EpicGames\Unreal Engine` 아래 버전 키의 `InstalledDirectory` 를 읽어(64비트 뷰 다음 32비트 뷰. 읽기만 한다) 버전 순으로 낸다. 키가 없거나 Windows 가 아니면 빈 배열이고, 소스에서 빌드한 엔진은 나오지 않는다. `disk` 는 `{path, free_bytes, total_bytes}` — `runs_dir` 가 놓인 드라이브다. `providers` 는 기존 `/providers` 응답 그대로다. `editors` 는 `[{project_id, pid, mcp_port, healthy, uproject}]` — 실행 중인 `UnrealEditor` · `UE4Editor` 프로세스(`-Cmd` 는 제외)이고, 명령줄의 `.uproject` 가 등록된 프로젝트의 `path` 아래면 그 프로젝트다(못 찾으면 `project_id` · `mcp_port` 는 `null`). `healthy` 는 그 프로젝트의 `mcp_port` 로 MCP `health_check` 를 보내 3초 안에 JSON 객체가 돌아오는지다 — 응답이 `status: error` 여도 참이고, 모달 · 크래시로 답이 없으면 거짓이다 (M-7 확정. `machine.py` 의 `health_check`. 전송 형식은 아래 '에디터 경로'). `limits` 는 `{max_concurrent_runs, max_exchanges, group_max_members, group_max_exchanges, hire_backlog_limit, hire_cooldown_days}` (§3.16. `group_max_exchanges` 는 단체방의 직원 간 왕복 한도 `Config.studio_group_max_exchanges` 다 — 앱이 방의 `exchange_count` 와 함께 n/한도 를 보이는 자리다, §4.6. `max_exchanges` 는 S-9 가 설정을 넣기 전까지 20. `group_max_members` 는 단체방의 직원 상한 `Config.studio_group_max_members` 다 — 앱이 초대할 수 있는 수를 미리 아는 자리다). 출처 하나를 읽지 못해도 응답은 200 이고 그 칸만 빈 배열이다(`disk` 는 두 값이 `null`). 캐시는 없다 — 부를 때마다 `claude --version` 한 번과 프로세스 조회(PowerShell 한 번, 최대 8초)를 돌리므로 몇 초 걸릴 수 있다. 그래서 앱은 이 값을 보여 주는 화면을 열거나 새로 고칠 때만 부르고, 값싼 네 값(`name` · `server_version` · `schema_version` · `utc_offset_minutes`)은 `hello` 의 `machine` (§7.1) 에서 읽는다 (M-1 확정). `/providers` 응답의 전체 모양은 확인 필요 (§11.3).
- 한도 읽기 · 바꾸기 (V-4 확정, `studio/limits.py`). `GET /studio/machine/limits` 는 `GET /studio/machine` 의 `limits` 중 바꿀 수 있는 셋을 가볍게 읽는다 — 그쪽은 진단(최대 8초)을 돌린다. `PATCH` 는 이름을 준 것만 바꾸고(`extra` 금지, 하나도 없으면 422) 값은 정수만 받는다: `max_runs` 1–20 (`studio_max_runs`), `max_exchanges` 1–200 (`studio_max_exchanges`), `group_max_exchanges` 1–100 (`studio_group_max_exchanges`). 0 · 음수 · 문자 · 불리언 · 범위 밖은 422 다. 성공하면 `relay.config.local.yaml` 에 적고 실행 중인 설정에 넣어 큐와 왕복 검사에 바로 적용하며(큐를 다시 본다), 응답은 바꾼 뒤의 세 값이다. 파일을 쓰지 못하면 500 이고 아무것도 바뀌지 않는다. 자세한 뜻은 §4.6.

**프로젝트 · 에디터 · 생성 작업**

| 메서드 · 경로 | 요청 | 응답 | 오류 | 작업 |
|---|---|---|---|---|
| `GET /studio/projects` | `?archived=0·1` | 행 목록 | | S-3 |
| `POST /studio/projects` | 가져오기: `{path, name, repo}` (`name` · `repo` 는 선택) | 201 행 + `{detected}` | `conflict` · `precondition_failed` | M-6 |
| `POST /studio/projects/create` | 새로 만들기: `{name, engine_version, parent_dir, plugins[], harness}` (`plugins` · `harness` 는 선택) | 202 `{job}` | `conflict` · `precondition_failed` | M-3–M-5 · M-8 |
| `GET /studio/projects/create/options` | `?engine_version=` (선택) | `{engines[], engine_version, plugins[], harness}` | `precondition_failed` | M-8 |
| `GET /studio/projects/{id}` | — | 행 + `{employees[], task_counts, editor}`. `employees[]` 는 `GET /studio/employees?project_id` 와 같은 목록이다(재직 인사 포함) | | S-3 |
| `PATCH /studio/projects/{id}` | `{name, engine_version, mcp_port}` 중 일부 | 행 | `conflict` | S-3 |
| `POST /studio/projects/{id}/archive` | — | 행 | `precondition_failed` (끝나지 않은 작업이 있다) | S-3 |
| `GET` · `PUT /studio/projects/{id}/policy` | PUT: `{text, base_sha256}` | `{text, sha256, updated_at}` | `precondition_failed` | H-6 |
| `GET /studio/projects/{id}/editor` | — | `{running, pid, healthy, checked_at}` | | M-7 |
| `POST /studio/projects/{id}/editor/start` · `/stop` | `stop`: `{force}` (선택, 기본 `true`) | 위와 같다 | `precondition_failed` | M-7 |
| `POST /studio/projects/{id}/harness` | — (owner 용. 하네스가 이미 깔려 있어도 돈다) | 200 `{project_id, status, detail, harness}` — `status` 는 `done` · `failed` | `not_found` · `precondition_failed` (하네스를 찾지 못했다) | T3 |
| `POST /studio/projects/{id}/editor/screenshot` | `{room_id}` (선택. 주면 그 방에 글로 올린다) | 201 `{attachment, message}` | `precondition_failed` | M-7 |
| `GET /studio/jobs/{id}` | — | `{id, kind, status, steps[], project_id, error, …}` (job 파일 그대로. 아래) | `not_found` | M-8 |
| `POST /studio/jobs/{id}/retry` · `/cleanup` | — | 202 `{job}` | `not_found` · `precondition_failed` · `conflict` | M-8 |

- 하네스 자동 세팅 (T3. `studio/harness_setup.py`). 프로젝트가 가져오기(`POST /studio/projects`)나 새로 만들기(job 의 `register`)로 등록되면 `api._bring_in` 이 `importer.harness_ready` 가 거짓일 때 한 번 `<하네스>/skills/game-bootstrap/scripts/bootstrap.py --auto <폴더>` 를 돌린다(`--auto` 는 있는 파일을 덮어쓰지 않는다. 300초 제한. 서버도 세팅 전에 폴더 바로 아래 `CLAUDE.md` · `CLAUDE.local.md` 를 저장해 두고 바뀌었으면 되돌린다 — 결과 `detail` · 글에 '기존 파일 되돌림: …' 이 붙는다). 이미 깔려 있으면 돌지 않고 글도 없다. 결과는 프로젝트 HR 1:1 방의 시스템 글 한 줄(`meta.event = harness_setup`, `project_id`, `status`)로 남는다 — 완료 · 실패(이유) · 하네스가 없어 건너뜀. 실패해도 가져오기는 201 이다. 세팅 글이 정책 면담 글보다 먼저다. 이미 등록된 프로젝트는 건드리지 않는다 — owner 가 `POST /studio/projects/{id}/harness` 로 직접 돌린다(깔려 있어도 돈다. 하네스가 없으면 `precondition_failed`, 없는 id 는 `not_found`, 세팅이 실패하면 200 에 `status: failed`). 새로 만들기의 `harness` 단계(bat)는 그대로이고, 그 뒤 등록 때 깔려 있지 않을 때만 이 세팅이 돈다.
- `POST /studio/projects` (M-6 이 정했다. `studio/importer.py`). 이미 있는 UE 프로젝트 폴더를 읽어 `projects` 행으로 등록한다. 폴더는 읽기만 한다 — 파일을 만들거나 고치지 않고 git 명령도 돌리지 않는다(`git status` 는 `.git/index` 를 고칠 수 있다). 빠진 MCP · 하네스는 `detected.missing` 으로 알리기만 하고, 채우는 일은 M-4 · M-5 가 한다. `path` 는 `.uproject` 가 있는 폴더의 절대 경로다. 저장소는 `RepoRegistry.match(path)` 가 돌려주는 것이고(§3.3), `repo` 를 주면 `RepoRegistry.get(repo)` 로 확인한 뒤 그 둘이 같아야 한다. 등록된 저장소 안의 폴더가 아니면 `precondition_failed` 다 — `relay repo add <이름> <경로>` 로 먼저 등록한다. 이 검사는 폴더가 있는지보다 먼저 해서, 저장소 밖 경로는 있든 없든 같은 답을 받는다. 그 뒤 폴더가 없거나 그 바로 아래에 `.uproject` 가 없으면 `precondition_failed`(`failed` 는 `[]`)다. `path` 가 폴더가 아니라 파일이어도(`.uproject` 파일 자체 포함) 같다 — 받는 것은 폴더뿐이다. `path` · `name` · `repo` 가 공백뿐이면 `missing_fields` 다. 경로(슬래시 방향 · 대소문자 · 끝 슬래시는 무시) · 이름 · 저장소가 이미 있는 프로젝트(보관된 것 포함)와 같으면 `conflict` 이고 `field` 는 `path` · `name` · `repo` 중 이 순서로 먼저 걸린 것이다. `name` 을 주지 않으면 `.uproject` 파일 이름(확장자 제외)이고, 저장하는 `path` 는 심볼릭 링크 등을 푼 절대 경로다. `engine_version` 은 `.uproject` 의 `EngineAssociation` 이 `5.7` 같은 버전 꼴이면 그 값이고(그 엔진이 이 컴퓨터에 없어도 같다), GUID(소스 빌드)나 빈 값이면 `''` 다 — `PATCH` 로 고친다. `mcp_port` 는 `Config/DefaultEngine.ini` 의 `[UnrealMCP]` 구역 `Port` 가 먼저고(에디터 플러그인이 읽는 값), 그 값이 `null` 이면(없거나 포트 꼴이 아니면) `.mcp.json` 의 `mcpServers.<이름>.env.UNREAL_MCP_PORT` 다. 포트 꼴은 1–65535 의 정수나 그 수를 숫자로만 적은 문자열이다(따옴표 · 앞뒤 공백은 뗀다). 구역에 `Port` 가 여러 번 나오면 엔진이 읽는 대로 마지막 것이 세고, 그 값이 포트 꼴이 아니면 앞의 값으로 돌아가지 않고 ini 값은 `null` 이다. `.mcp.json` 쪽은 파일에 적힌 순서로 `UNREAL_MCP_PORT` 를 적은 첫 서버의 값이고, 그 값이 포트 꼴이 아니면 다음 서버를 보지 않고 `null` 이다. 그 포트를 보관 안 된 다른 프로젝트가 쓰고 있으면 거절하지 않고 `mcp_port` 를 `null` 로 등록한 뒤 `detected.mcp.in_use_by` 에 그 프로젝트 id 를 낸다. 응답은 201 로 행에 `detected` 를 얹은 것이고, `project.updated` 이벤트의 `project` 에는 행만 싣는다. `detected` 는 `{uproject, engine, mcp, harness, git, missing[]}` 다. `uproject` 는 파일 이름이다. `engine` 은 `{association, installed}` — `EngineAssociation` 그대로와, 그 버전이 `machine` 의 `engines` 에 있고 그 폴더가 있는지(`exists`)다. `mcp` 는 `{port, ini, mcp_json, in_use_by}` — 두 파일에서 읽은 값과 그중 고른 값이다(못 읽으면 `null`). `harness` 는 `{claude_md, docs}` — 릴레이 런이 읽는 규칙 파일(`CLAUDE.md` · `CLAUDE.local.md` · `.claude/CLAUDE.md`) 가운데 하나가 폴더에 파일로 있는지(`importer.rules`. 키 이름은 그대로 두고 뜻만 M-5 마무리에서 넓혔다)와 `Docs` 폴더가 있는지다. `git` 은 프로젝트 폴더에서 저장소 루트까지 올라가며 `.git` 이 있는지이고, `missing` 은 `mcp`(포트를 찾지 못했다) · `harness`(둘 중 하나라도 없다) 가운데 빠진 것이다 (M-6 확정).
- 생성 작업(job)은 12개 테이블에 없다. `runs/studio/jobs/<job_id>.json` 에 두고 id 는 `job_<hex8>` 이다 (M-8 확정. `relay_agent/studio/jobs.py`). 단계와 이벤트는 §7.4.
- 에디터 경로 (M-7 이 정했다. `studio/editor.py`). 없는 프로젝트는 `not_found` 이고, 거부는 모두 `precondition_failed` 에 `failed[]` 한 개다.
  - 상태 `{running, pid, healthy, checked_at}`. `running` 은 명령줄의 `.uproject` 가 이 프로젝트 폴더(`projects.path`) 아래인 에디터 프로세스가 있는지다 — `machine` 의 `editors` 와 같은 판정이다. `pid` 는 그 가운데 가장 작은 것이고 없으면 `null` 이다. `healthy` 는 `machine` 의 `editors` 와 같은 `health_check` 이고, 에디터가 꺼져 있거나 프로젝트에 `mcp_port` 가 없으면 묻지 않고 `false` 다. `checked_at` 은 본 시각이다 (§3.1 형식).
  - `start` 는 `<엔진 폴더>\Engine\Binaries\Win64\UnrealEditor.exe <.uproject>` 를 프로젝트 폴더에서 실행한다. 엔진 폴더는 그 프로젝트 `engine_version` 의 레지스트리 값이고 `.uproject` 는 프로젝트 폴더 바로 아래의 것이다. 이미 떠 있으면 실행하지 않는다. 응답을 기다리지 않고 바로 상태를 준다 — 에디터가 뜨는 데 몇 분이 걸리므로 앱이 `GET …/editor` 를 다시 읽는다. `failed[]`: `engine_unknown`(설치된 엔진이 아니다) · `engine_missing`(엔진 폴더가 없다) · `editor_missing`(`UnrealEditor.exe` 가 없다) · `uproject_missing` · `editor_start_failed`.
  - `stop` 은 이 프로젝트의 에디터 프로세스만 끝낸다 — 다른 프로젝트의 에디터와 등록되지 않은 에디터는 건드리지 않는다. `taskkill /PID` 로 닫으라고 한 뒤 15초 안에 꺼지지 않은 것만 `taskkill /T /F` 로 끝낸다. 그때 에디터의 저장하지 않은 변경은 잃는다. 본문은 없어도 되고 `{force}` 만 받는다. 주지 않으면 `true` 다 — 원격에서는 저장 확인 창을 누를 사람이 없어, 강제 종료가 기본이어야 실제로 꺼진다. `force: false` 면 강제 종료를 하지 않는다: 닫으라고 한 뒤 똑같이 15초까지 기다려 상태를 주고, 저장 확인 창 등으로 아직 떠 있으면 `running: true` 다. 에디터가 없으면 바로 상태를 준다.
  - `screenshot` 의 본문은 객체가 필수다(`{}` 도 된다). 순서: 프로젝트에 `mcp_port` 가 없으면 아무것도 실행하지 않고 `mcp_port_missing` → 에디터가 꺼져 있으면 `start` 와 같이 실행 → 2초 간격으로 `health_check` 를 300초까지 기다린다(넘으면 `editor_timeout`. 에디터는 그대로 둔다. 그동안 HTTP 요청이 열려 있다) → MCP `take_screenshot` 을 `filepath` 와 함께 보낸다(30초). 오류 응답이거나 답이 없으면 `screenshot_failed` 다. 다만 이 요청이 에디터를 켰거나 `health_check` 를 한 번이라도 기다렸으면 — 포트는 답해도 뷰포트가 아직 없을 수 있다 — 첫 전송부터 30초 동안 2초 간격으로 같은 `filepath` 로 다시 보내고, 그래도 안 되면 마지막 오류로 `screenshot_failed` 다. 처음부터 응답하던 에디터의 오류는 다시 보내지 않고 바로 알린다. 답이 온 뒤 파일이 없거나 비었으면 0.2초 간격으로 5초까지 다시 본다(플러그인이 다음 프레임에 쓸 수 있다). 그래도 없거나 비었거나, 25MB 를 넘으면 `screenshot_missing` 이다. 30초 · 5초는 재 보지 않은 추정값이다. 꺼진 에디터라면 `start` → `GET …/editor` 로 `healthy` 확인 → `screenshot` 순서가 요청을 오래 열어 두지 않는다. 파일 위치와 방에 올리는 글은 §6.6.
  - MCP 전송 형식 (잠정 — MNYS 의 플러그인 원본은 저장소 밖이라 읽지 못했고 공개 unreal-mcp 의 형식이다. VERIFICATION.md 의 M-7 줄이 확인한다): 배정 포트에 TCP 로 연결해 `{"type": <명령>, "params": {…}}` 를 UTF-8 JSON 으로 한 번 보내고, 받은 바이트가 JSON 객체 하나가 될 때까지 읽는다. 응답은 `{status, result}` 이고 실패는 `status: "error"` 다. 다르면 `machine.py` 의 `mcp_send` · `HEALTH_COMMAND` 와 `editor.py` 의 `SCREENSHOT_COMMAND` · `SCREENSHOT_PARAM` 을 고친다.
  - `GET /studio/projects/{id}` 의 `editor` 는 `null` 로 확정한다. 상태는 `GET /studio/projects/{id}/editor` 가 준다 — 상세 응답에 넣으면 조회마다 프로세스 목록을 읽게 된다.
  - 작업 런의 에디터 검사(§4.3 T3)는 같은 `Tools` 와 `bring_up` 을 쓴다. 응답 대기는 `Config.studio_editor_wait`(기본 300초)이고 검사할 때마다 읽는다. 런 도중의 감시 간격과 횟수는 `editor_watch.py` 의 `EVERY`(30초) · `MISSES`(3)다. 셋 다 실제 에디터로 재지 않은 추정값이다 (VERIFICATION.md 의 M-7 줄).
  - job 파일이 곧 `GET /studio/jobs/{id}` 의 응답이다: `{id, kind, status, steps[], project_id, error, request, engine_dir, path, repo, repo_new, mcp_port, created_at, updated_at, finished_at}`. `kind` 는 `project_create`, `status` 는 `pending` → `running` → `done` · `failed` 다 (`cleaned` 는 정리 뒤). `repo_new` 는 `register` 단계가 돌기 직전에 적는 값으로, 그 이름의 저장소 등록이 그때 없었는지다 (`null` 은 아직 모름. 정리는 `true` 일 때만 등록을 지운다). `steps[]` 는 다섯 단계 순서대로 `{step, status, error, tail[], created[], started_at, ended_at}` 이고, 그 `status` 는 `pending` · `running` · `done` · `failed` · `skipped`, `tail` 은 그 단계 로그의 마지막 40줄, `created` 는 그 단계가 만든 것의 절대 경로다 (정리가 쓴다). `request` 는 검사를 통과한 요청이고 생략한 `plugins` · `harness` 는 채워져 있다. `path` 는 만들 폴더, `repo` · `mcp_port` · `project_id` 는 그 값을 정하는 단계가 끝나야 채워진다. 없는 값은 `null` 이다. 파일은 임시 파일을 쓴 뒤 바꿔치기로 쓰고, 이벤트보다 먼저 쓴다. 없는 id 는 `not_found` 다.
  - `POST /studio/projects/create` 는 검사만 하고 202 로 `pending` 인 job 을 돌려준다 (`jobs.admit`). 단계는 job 의 스레드가 돈다. 검사 순서는 `creator.check`(이름 → 부모 폴더 → 이미 있는 폴더 → 엔진) → 플러그인 → 하네스 → 등록된 프로젝트 · 저장소와의 충돌 → 끝나지 않은 생성 작업이다. 거부 본문은 `{detail, code, field, reason}` 이다. `conflict`: 폴더가 이미 있다 (`name` · `folder_exists`), 프로젝트(보관된 것 포함)가 그 폴더 · 이름 · 저장소를 쓴다 (`importer.conflict`. `path` · `name` · `repo` 와 `path_taken` · `name_taken` · `repo_taken`), 저장소 이름이 다른 경로로 등록돼 있다 (`repo` · `repo_taken`), 그 폴더가 다른 이름의 저장소다 (`path` · `path_taken`), 같은 이름 · 폴더를 만드는 job 이 끝나지 않았다 (`name` · `job_running`). `precondition_failed`: 이름 · 부모 폴더 · 엔진 (M-3 의 `reason`), 모르는 플러그인 · 원본이 없는 플러그인 (`plugins`), 설정이 틀려 `catalog` 가 실패했다 (`plugins` · `plugins_misconfigured` — 고칠 사람이 owner 라 원문을 `detail` 에 싣는다), `harness` 가 `true` 인데 하네스 저장소를 쓸 수 없다 (`harness` · `harness_unavailable` — 몇 분짜리 빌드 뒤에 실패하지 않게 미리 막는다).
  - 생략값: `plugins` 를 빼면 선택지에서 `checked` 인 것이고, `harness` 를 빼면 하네스 저장소를 쓸 수 있는지다. `[]` 와 `false` 는 명시한 값이다.
  - `GET /studio/projects/create/options` 는 생성 화면의 선택지다 (`jobs.options`). `engines[]` 는 `{version, path, exists, usable, warning}` — `usable` 은 그 엔진으로 만들 수 있는지(`creator.engine_problem`), `warning` 은 못 쓰는 이유다. `engine_version` 은 `plugins[]` 를 계산한 엔진이다: 쿼리로 준 것, 없으면 쓸 수 있는 것 중 가장 높은 버전, 그것도 없으면 `""`. `plugins[]` 는 `choices` 의 `{id, name, checked, warning, available}`, `harness` 는 `{available, checked, warning}` 이다.
  - 생성 작업은 한 번에 하나만 돈다. 기다리는 job 은 `pending` 이고, 기다리는 것끼리의 차례는 보장하지 않는다. `plugins` 단계의 `taken` 은 모든 프로젝트(보관 포함)의 `mcp_port` 에, `done` · `cleaned` 가 아닌 다른 job 파일의 `mcp_port` 를 더한 것이다 — 실패한 job 의 폴더에 적힌 포트를 다음 job 이 받지 않는다.
  - 단계 함수가 예외를 내면 그 단계의 실패다 (`error` 에 예외 이름). 서버 연결은 `api.use_jobs(reload=, local_file=)` 이다: `server.py` 가 `_reload_repos` 뒤 `engine.repos` 를 돌려주는 함수와 `ROOT/relay.config.local.yaml` 을 넘긴다. 테스트는 그 자리에 임시 설정 파일과 아무것도 실행하지 않는 실행기를 넘긴다. `_reload_repos` 는 `server._repos_lock` 안에서 설정을 읽고 `cfg.repos` · `engine.repos` 를 바꾼다 — job 스레드와 `/repos` 요청 스레드가 겹쳐도 먼저 읽은 낡은 목록이 나중 것을 덮지 않는다. 설정 파일을 쓰는 쪽(`save_local_repo`)은 이 잠금 밖이다 — 대시보드의 저장소 등록과 `register` 단계가 같은 순간에 파일을 쓰는 경우는 막지 않는다.
  - 재시도 (`POST /studio/jobs/{id}/retry`. `Jobs.retry`, M-8 확정). `failed` 인 job 만 받는다 — 아니면 `precondition_failed` (`status` · `job_not_failed`), 없는 id 는 `not_found` 다. 202 로 `pending` 이 된 job 을 돌려주고 job 의 스레드가 다시 돈다. `done` · `skipped` 인 단계는 건너뛰고 실패한 단계부터 돈다. 다시 도는 단계는 앞선 시도가 남긴 `created` 를 먼저 지운다(`folder` 와 `plugins` 는 이미 있는 대상을 실패로 본다) — 그래서 `build` 는 처음부터 다시 빌드한다. 못 지우면 그 단계의 실패다. 요청은 job 파일의 `request` · `engine_dir` 로 다시 만들고(`jobs.readmit`) `creator.check` 는 하지 않는다 — 폴더가 이미 있기 때문이고, 그래서 서버가 다시 시작된 뒤에도 재시도할 수 있다. 설정이 바뀌어 고른 플러그인이 없으면 M-4 의 `reason` 으로 거부한다. 이 job 이나 같은 이름 · 폴더의 다른 job 이 끝나지 않았으면(정리 중 포함) `conflict` (`job_running`) 다.
  - 정리 (`POST /studio/jobs/{id}/cleanup`. `Jobs.cleanup`, M-8 확정). `failed` 인 job 만 받고(`job_not_failed`), 요청 안에서 끝낸다. 이 job 이 만든 것만 지운다: 프로젝트 행(`project_id`), 저장소 등록(`repo_new` 가 `true` 이고 그 이름이 아직 이 폴더를 가리킬 때. `save_local_repo(remove=True)` 뒤 설정을 다시 읽는다), 각 단계의 `created` 에 적힌 경로다. 경로는 job 의 `path` 와 그 아래만 지운다 — 글자로 비교하고 `..` 이 든 경로는 건드리지 않는다. 링크(정션 포함)는 링크만 끊고, 적힌 폴더는 안에 든 것째로 지운다. 프로젝트 폴더 자신은 다르다 — 빈 하위 폴더를 걷어낸 뒤 비어 있을 때만 지운다. 어느 단계도 적지 않은 것(실패 뒤 사람이 넣은 파일, 에디터가 만든 `Saved` 등)이 있으면 그것과 폴더가 남고 `error` 에 그 이름을 다섯 개까지 적는다. 단계가 적은 파일은 사람이 고쳤어도 지운다. 재시도가 앞선 시도의 `created` 를 지울 때도 같은 규칙이다. 다 지우면 job 은 `cleaned` 이고 `created` 는 모두 `[]` 다. 못 지운 것이 있으면 `failed` 그대로이고 `error` 에 남은 것을 적으며, 모든 단계를 `pending` 으로 되돌린다 — 그 뒤의 재시도는 처음부터 다시 만든다. 프로젝트 행에 딸린 것(직원 · 작업 · 방 등)이 있으면 아무것도 지우지 않고 `precondition_failed` (`project_id` · `project_in_use`) 다. 다른 job 이 돌고 있거나 이 job 이 재시도 · 정리 중이면 기다리지 않고 `conflict` (`status` · `job_running`) 다. `register` 가 행을 넣은 직후 중단된 job 은 파일에 `project_id` 가 없다. 재시도와 정리는 그 job 을 잡은 뒤에 그 행(이름 · 폴더가 같고 그 단계가 시작한 뒤에 생긴 것)을 찾아 파일에 적는다 — 정리 중에 온 재시도는 파일을 쓰지 않고 `job_running` 으로 끝난다. 재시도는 행을 다시 넣지 않고 그 단계가 만든 것(`.git` 등)도 지우지 않으며, 정리는 그 행을 지운다.
  - 재시작 (`api.recover_jobs()`. `Jobs.recover`, M-8 확정). 서버가 시작할 때(`server.recover_studio_jobs`) `pending` · `running` 으로 남아 있는 job 파일을 `failed` 로 바꾼다. `error` 는 "생성 작업이 중단됐다 (서버가 다시 시작됐다)" 이고 돌던 단계도 같은 글로 `failed` 다. 그 뒤 재시도하거나 정리할 수 있다. 중단된 단계가 남긴 것도 `created` 에 적힌다. `folder` 단계는 폴더가 아직 없을 때 만들기 전에 그 폴더를 먼저 적는다. 중단을 실패로 적을 때(재시작, job 파일을 쓰지 못한 경우, 단계의 예외)는 프로젝트 폴더 바로 아래에서 어느 단계도 적지 않은 것을 돌던 단계의 `created` 에 더한다 — `folder` 단계가 그 폴더를 적었을 때만이다. 그래서 그렇게 끊긴 `folder` · `plugins` 도 재시도하거나 정리할 수 있다. 단계 전에 이미 있던 폴더는 적지 않는다: `folder` 는 "이미 있는 폴더"로 실패하고 정리는 그 폴더를 건드리지 않는다. 중단과 재시작 사이에 사람이 폴더 바로 아래에 넣은 것은 돌던 단계의 것으로 적힌다.
  - 명령의 제한 시간 (`creator.run_process`). 명령 하나가 `creator.TIME_LIMIT`(3600초 — 실제 첫 빌드로 재지 못한 추정값)을 넘기면 그 명령이 띄운 프로세스까지 끝내고(Windows 는 `taskkill /T /F`) 종료 코드 `creator.TIMED_OUT`(124)을 돌려준다. 그 단계는 "exit 124" 로 실패하고 `created` 와 로그 꼬리는 남는다. 명령이 끝났는데 출력을 쥔 프로세스가 남아 있으면 남은 줄은 `creator.GRACE`(5초)만 기다리고 그 뒤에 온 줄은 버린다. 아직 없는 것: 서버가 죽을 때 돌던 명령을 끝내는 것 — 남은 명령이 폴더를 쓰는 동안의 재시도와 정리는 다 지우지 못해 실패할 수 있다.
- `POST /studio/projects/create` 의 앞 두 단계 `folder` · `build` (M-3 이 정했다. `studio/creator.py`). 라우트 · job · 이벤트는 M-8 이 만들고, 여기에는 검사와 단계 함수만 있다.
  - 엔진 `Templates/` 는 쓰지 않는다. 엔진 폴더를 읽어 확인하려 했으나 릴레이 단계에서 엔진 폴더 읽기가 두 번 거부되어, 에디터 없이 `Templates/` 만으로 C++ 프로젝트를 CLI 생성할 수 있는지는 **확인하지 못했다**. 불확실하므로 저장소의 스켈레톤 `relay_agent/studio/template/` 을 쓴다 (CAMPAIGN.md M-3). 확인되면 이 줄을 고친다.
  - 검사 `check(name, engine_version, parent_dir)` 는 읽기만 하고, 거부는 `Refused(code, field, reason)` 다. 순서대로: 이름이 영문자로 시작하는 영문자 · 숫자 · 밑줄 20자 이내가 아니면 `name_invalid`(공백 · 한글 포함), 윈도 장치 이름이나 엔진 모듈 · 타깃 이름이면 `name_reserved` → 부모 폴더가 절대 경로가 아니면 `parent_not_absolute`, 경로에 영문자 · 숫자 · `_` `-` `.` `~` 와 구분자(`\` `/`, 맨 앞 드라이브 글자 뒤의 `:`) 말고 다른 글자가 있으면 `path_invalid`(공백 · 한글, 그리고 `&` `^` `%` 같은 cmd 특수 문자 — `Build.bat` 이 cmd 를 거쳐 `-project=` 인자가 잘리거나 뒤쪽이 명령으로 실행될 수 있다. 엔진 폴더 경로에는 이 검사를 하지 않는다 — 기본 설치 위치에 공백이 있고 값이 요청이 아니라 레지스트리에서 온다), 폴더가 아니면 `parent_missing` → `부모/이름` 이 이미 있으면 `folder_exists` → 엔진이 `machine.engines()` 에 없으면 `engine_unknown`, 있어도 `exists` 가 거짓이면 `engine_missing`(레지스트리에 키만 남은 4.27 등), 5.0 미만이면 `engine_unsupported`, `Engine/Build/BatchFiles/Build.bat` 이 없으면 `engine_incomplete`. `folder_exists` 만 `conflict` 이고 나머지는 `precondition_failed` 다.
  - 템플릿의 자리표시는 `__NAME__` · `__ENGINE__` 둘이고 파일 이름과 내용에서 한 번에 치환한다. 템플릿에 모르는 자리표시가 있으면 `folder` 가 실패한다. Target 둘은 `BuildSettingsVersion.Latest` · `EngineIncludeOrderVersion.Latest` 라 5.6 · 5.7 을 한 템플릿으로 덮는다. `.gitignore` · `CLAUDE.md` · `[UnrealMCP]` 구역은 템플릿에 없다 (M-4 · M-5).
  - 명령 줄 둘은 엔진의 `Engine/Build/BatchFiles/Build.bat` 하나로 부른다 (잠정 — 5.7 실제 빌드 전이다, VERIFICATION.md). 프로젝트 파일 생성: `Build.bat -projectfiles -project=<.uproject> -game -rocket -progress`. 에디터 타깃 빌드: `Build.bat <이름>Editor Win64 Development -project=<.uproject> -waitmutex`. 작업 폴더는 프로젝트 폴더이고, 앞의 것이 0 으로 끝나야 뒤의 것을 돌린다.
  - 단계 함수 `folder(spec, run=, log=)` · `build(spec, run=, log=)` 는 실행기(`run(argv, cwd=, log=) -> exit code`)와 로그 콜백(`log(line)` — `job.log` 의 `lines[]` 가 된다)을 주입받고, 예외 대신 `StepResult{step, created[], error, tail[]}` 를 돌려준다. `created` 는 실패했을 때에도 그때까지 만든 것의 절대 경로이고(`folder`: 프로젝트 폴더와 파일들, `build`: 프로젝트 폴더 바로 아래에 새로 생긴 것), `tail` 은 로그의 마지막 40줄이다. 두 단계 모두 아무것도 지우지 않는다 — 반쯤 만든 폴더의 정리는 M-8 이 `created` 로 한다. 기본 실행기(`creator.run_process`)는 `creator.TIME_LIMIT` 을 넘긴 명령을 그 프로세스 트리째 끝내고 `creator.TIMED_OUT` 을 돌려준다 (위 '명령의 제한 시간').
- `POST /studio/projects/create` 의 `plugins` 단계 (M-4 가 정했다. `studio/plugins.py`): 복사와 `.uproject` 등록, 그리고 MCP 포트 배정 · `[UnrealMCP]` · `.mcp.json` · `mcp_guards.json`. 라우트 · job · 이벤트는 M-8 이 만든다.
  - 설치 목록은 `Config.studio_plugins` 다. 항목은 `{id, name, source, folder, engines[]}` 이고 기본은 `unreal_mcp`(Unreal MCP, `Plugins/Unreal-MCP-main`) · `ys_tag_usage`(YSTagUsage, `Plugins/YSTagUsage`, `engines: ["5.7"]`) 둘이다. `id` 는 요청의 `plugins[]` 가 부르는 값이다. `source` 가 절대 경로가 아니면 `Config.studio_plugin_root`(MNYS 폴더의 절대 경로 — PC 마다 달라 `relay.config.local.yaml` 에 둔다)에 붙인다. root 가 비어 있으면 그 항목은 원본이 없는 것으로 본다. `folder` 는 프로젝트의 `Plugins/` 아래 폴더명이고 없으면 `source` 의 마지막 마디다. `engines` 는 확인된 엔진 버전이고 빈 목록은 전부다. 설정이 틀리면(`id` · `source` 누락, `id` 중복, 한 마디가 아닌 `folder`) `catalog` 가 `ValueError` 를 낸다.
  - 생성 화면의 선택지는 `choices(catalog, engine_version)` → `[{id, name, checked, warning, available}]` 다. `available` 은 원본 폴더가 있는지, `checked`(기본 체크)는 원본이 있고 그 엔진에서 확인됐는지다. 엔진이 `engines` 에 없으면 기본 해제하고 `warning` 에 확인된 버전과 고른 버전을 적는다 — YSTagUsage 는 5.7 이 아니면 그렇게 나온다. 원본이 없거나 root 가 미설정이어도 `warning` 에 적는다. 이것을 내려 주는 경로는 `GET /studio/projects/create/options` 다 (M-8).
  - 요청 검사는 `select(catalog, ids)` 다. 목록에 없는 id 는 `plugin_unknown`, 원본 폴더가 없으면 `plugin_source_missing` 이고 둘 다 `Refused("precondition_failed", "plugins", …)` 다. 확인되지 않은 엔진은 거부하지 않는다 — 경고를 보고 고른 것이다. 결과는 목록 순서이고 중복은 하나로 합친다.
  - `install(spec, chosen, run=, log=)` 는 단계의 앞부분(복사와 등록)이다. `folder` · `build` 와 같은 모양이고 `StepResult("plugins")` 를 돌려준다. 고른 플러그인마다 원본 폴더를 `<프로젝트>/Plugins/<folder>` 로 복사하는데 이름이 `.git` 인 것(폴더와 파일)과 `.zip` 으로 끝나는 것은 뺀다. `Binaries` · `Intermediate` 는 원본에 있으면 그대로 복사된다. 복사본의 읽기 전용 속성은 푼다(파일 · 폴더, 중간에 실패한 복사본 포함) — 빌드가 덮어쓰고 M-8 정리가 `created` 를 지울 수 있어야 한다. 원본의 속성은 건드리지 않는다. 원본은 읽기만 한다. 대상 폴더가 이미 있으면 건드리지 않고 실패하고, 원본이 없거나 복사본에 `.uplugin` 이 없어도 그 자리에서 멈춘다. `.uproject` 는 모든 복사가 끝난 뒤 한 번만 쓰므로 실패한 단계는 `.uproject` 를 바꾸지 않는다. `created` 는 폴더 단위다 — `Plugins/`(이 단계가 만들었을 때), 그다음 복사한 폴더들(복사가 중간에 실패한 폴더 포함). 고른 것이 없으면 아무것도 만들지 않고 성공한다.
  - `.uproject` 등록 `register(uproject, names)` 는 `Plugins` 에 `{"Name": <이름>, "Enabled": true}` 를 붙인다. 이름은 폴더명이 아니라 복사본에서 찾은 `.uplugin` 파일의 이름이다 — `.uplugin` 이 있는 폴더는 그 파일을 쓰고 더 내려가지 않으며, 없으면 하위 폴더를 이름순으로 본다. 같은 이름이 이미 있으면(대소문자 무시) `Enabled` 만 켠다. 탭 들여쓰기 · 원래의 줄 끝 · BOM · 끝 줄바꿈을 지키고, 바뀐 것이 없으면 쓰지 않는다.
  - 플러그인 폴더명이 `UnrealMCP` 여야 하는지는 **확인하지 못했다** (CAMPAIGN.md 의 '검증 안 됨'). 원본 `MNYS/Plugins/Unreal-MCP-main` 의 `.uplugin` 과 모듈을 읽어 확인하려 했으나 릴레이 단계에서 저장소 밖 폴더 읽기가 거부됐다. 그래서 잠정으로 원본 폴더명 그대로(`Unreal-MCP-main`) 복사한다 — 그 이름으로 도는 것이 알려진 구성은 MNYS 뿐이다. 엔진이 플러그인을 `.uplugin` 파일 이름으로 식별한다는 것도 이 저장소에서 확인한 사실이 아니라 전제다. 이 저장소가 아는 `UnrealMCP` 는 `DefaultEngine.ini` 의 구역 이름과 `Saved/UnrealMCP/port.txt` 뿐이고 둘 다 플러그인 폴더명이 아니다. 소스가 폴더명을 문자열로 참조하는 것으로 드러나면 코드가 아니라 설정의 `folder: UnrealMCP` 로 고친다. 확인 방법은 VERIFICATION.md 의 "M-4 플러그인 폴더명" 줄이다.
  - 단계 함수는 `step(spec, chosen, taken=, is_open=, run=, log=)` 이다. `install` 을 돌리고, 성공했고 고른 것 가운데 설정 항목에 `mcp` 가 있는 플러그인이 있으면(여럿이면 첫째) 포트를 배정해 파일 셋을 쓴다. 돌려주는 것은 `PluginsResult` — `StepResult` 에 `mcp_port` 를 더한 것 — 이고, M-8 이 그 값을 `projects.mcp_port` 에 싣는다. `mcp` 플러그인을 고르지 않았으면 포트를 배정하지 않고 파일 셋도 쓰지 않는다(`mcp_port` 는 `null`). 복사가 실패하면 거기서 멈춘다. 설정 쓰기가 실패하면 `error` 는 "MCP 설정을 쓰지 못했다: …" 이고 `mcp_port` 는 `null` 이며 아무것도 지우지 않는다. `created` 에는 `install` 의 것 뒤에 여기서 새로 만든 것(`.mcp.json`, `.claude/`, `mcp_guards.json`)만 붙는다 — `Config/DefaultEngine.ini` 는 `folder` 단계가 만든 것을 고쳐 쓰므로 없었을 때만 들어간다.
  - 포트 배정 `allocate(taken, is_open=)`: 55557 은 MNYS 몫이라 내주지 않는다. 55558 부터 올라가며 `taken` 에 있는 것과 이 컴퓨터에서 열려 있는 것(`machine.port_open`)을 건너뛴 첫 포트다. `taken` 은 호출자(M-8)가 넘기는, 등록된 프로젝트의 `mcp_port` 전부다 — 보관된 것도 넣어(`list_projects(include_archived=True)`) 보관을 풀 때 겹치지 않게 한다. `taken` 에 있는 포트에는 접속해 보지 않는다. 65535 까지 남은 것이 없으면 `ValueError` 다. 동시에 도는 두 생성 작업이 같은 포트를 받지 않게 하는 것은 M-8 의 일이다 — 직렬화하거나 진행 중인 작업의 포트를 `taken` 에 넣는다.
  - `Config/DefaultEngine.ini` (`write_ini_port`): `[UnrealMCP]` 구역의 마지막 `Port=` 줄이 있으면 그 줄만 바꾸고, 구역만 있으면 머리줄 바로 뒤에 넣고, 구역이 없으면 파일 끝에 빈 줄 · `[UnrealMCP]` · `Port=<포트>` 를 붙인다. 다른 구역과 줄, 줄 끝(CRLF · LF), 인코딩(UTF-8 · 표식이 있는 UTF-16)과 BOM 은 그대로이고, 바뀐 것이 없으면 쓰지 않는다. 그 인코딩의 글이 아니면 `ValueError` 이고 파일은 그대로다. `Port=` 줄을 바꿀 때 `=` 뒤는 앞 공백만 남기고 전부 새 포트가 되어 같은 줄의 `; 주석` 은 사라진다 — `importer.ini_port` 가 숫자만 있는 값을 포트로 읽으므로 주석을 남기면 되읽기가 실패한다. 엔진이 값 뒤의 `;` 를 주석으로 보는지는 확인하지 못했다.
  - `.mcp.json` (`write_mcp_json`): `mcpServers.<이름>` 에 `{command, args, env: {"UNREAL_MCP_PORT": "<포트>"}}` 를 쓴다. 포트는 숫자를 적은 문자열이다. 프로젝트의 하위 폴더나 복사본에서 돌면 `Saved/UnrealMCP/port.txt` 를 찾지 못하므로 포트를 환경 변수로 고정한다 (CAMPAIGN.md M-4). 파일이 이미 있으면 다른 서버, 그 항목의 다른 키와 다른 `env`, 들여쓰기 단위(`{` 다음 줄의 탭 · 칸 수, 한 줄짜리면 2칸) · 줄 끝 · BOM 을 지킨 채 합치고(새 파일은 2칸 · LF 다), JSON 객체가 아니면 `ValueError` 이고 파일은 그대로다. 서버 항목은 설정 항목의 `mcp` — `{server, command, args[]}`, `server` 를 빼면 `unreal` — 이고 틀리면(`command` 누락, 문자열 목록이 아닌 `args`) `catalog` 가 `ValueError` 를 낸다. `{plugin}`(`<프로젝트>/Plugins/<folder>`) · `{project}` 는 절대 경로(`/` 구분)로 바꿔 쓴다. 기본값 `uv --directory {plugin}/Python run unreal_mcp_server.py` 는 **확인하지 못했다** — MNYS 의 `.mcp.json` 이 저장소 밖이라 읽지 못하고 일반 지식으로 적은 잠정값이다. 틀리면 코드가 아니라 설정을 고친다 (VERIFICATION.md). 절대 경로가 들어가므로 이 파일을 git 에 올릴지는 M-5 가 정한다.
  - `.claude/mcp_guards.json` (`write_guards`): 없을 때만 `relay_agent/studio/defaults/mcp_guards.json` 을 그대로 복사한다. 이미 있으면 건드리지 않는다. 기본본은 지금 규칙이 없는 `{}` 다 — MNYS 의 것을 읽지 못해 형식을 모르고, 규칙을 지어내지 않았다 (VERIFICATION.md).
  - 쓴 뒤 `importer.ini_port` · `mcp_json_port` 로 되읽어 배정한 포트와 다르면 단계 실패다 — 쓴 값은 나중에 그 폴더를 가져올 때(`detect`) 읽히는 값과 같아야 한다. `.mcp.json` 에 `UNREAL_MCP_PORT` 를 다른 값으로 적은 다른 서버가 앞에 있으면 그렇게 된다 (다른 서버는 고치지 않는다).
  - 주의 (미결 — M-8 은 순서를 그대로 뒀다): §7.4 의 순서는 `build` → `plugins` 라, 복사한 플러그인의 C++ 모듈이 빌드되지 않은 채 에디터가 처음 뜬다. 순서를 바꾸거나 `plugins` 뒤에 다시 빌드해야 하는데, 어느 쪽인지는 5.7 로 실제 빌드를 해 봐야 정할 수 있다. 순서는 `jobs.STEPS` 하나로 정해진다. 함께 미결이던 두 가지는 M-8 이 정했다: 요청에서 `plugins` 를 생략하면 `choices` 의 `checked` 인 것을 넣고, 설정이 틀려 `catalog` 가 `ValueError` 를 내면 `precondition_failed`(`field` 는 `plugins`)다.
- `POST /studio/projects/create` 의 `harness` · `register` 단계 (M-5 가 정했다. `relay_agent/studio/harness.py`). 라우트 · job · 이벤트는 M-8 이 만든다. 두 단계 함수는 `folder` · `build` 와 같은 모양이고(실행기 · 로그 콜백 주입, 예외 대신 결과) 아무것도 지우지 않는다.
  - `StepResult` 에 `skipped`(기본 `false`)가 더해졌다. 건너뛴 단계는 `ok` 이면서 `skipped` 이고, M-8 이 그것을 `job.step` 의 `status` `skipped` 로 옮긴다 (§7.4).
  - `harness(spec, harness_root, enabled=, run=, log=)`: `enabled`(요청의 `harness`)가 거짓이면 아무것도 돌리지 않고 `skipped` 다 — 하네스 경로가 비어 있어도 실패가 아니다. 켜져 있으면 game-harness 저장소의 `bootstrap.bat <프로젝트 폴더>` → `index_build.bat <프로젝트 폴더>` 를 돌린다. bat 은 `cmd /c` 없이 그 파일을 직접 실행하고 작업 폴더는 하네스 저장소이며, 앞의 것이 0 으로 끝나야 뒤의 것을 돌린다. 하네스 저장소 경로는 `Config.studio_harness_root` 다 (절대 경로. PC 마다 달라 `relay.config.local.yaml` 에 둔다). 값이 비었거나 절대 경로가 아니거나, 그 폴더나 두 bat 중 하나나 프로젝트 폴더가 없으면 명령을 하나도 돌리지 않고 실패하고, `error` 에 설정 이름이나 없는 경로를 적는다. 둘 다 0 으로 끝났는데 릴레이 런이 읽는 규칙 파일(`projctx.INSTRUCTION_FILES` — `CLAUDE.md` · `CLAUDE.local.md` · `.claude/CLAUDE.md`)이 프로젝트에 하나도 없으면 실패다 — 규칙 파일이 없으면 하네스가 꺼져 있다. `created` 는 프로젝트 폴더 바로 아래에 새로 생긴 것이다. bat 의 인자가 폴더 하나인지, 하네스 저장소에서 실행해도 되는지, 만드는 규칙 파일이 어느 것인지는 **확인하지 못했다** — game-harness 가 저장소 밖이라 읽지 못했다 (VERIFICATION.md).
  - `register(spec, store, local_file=, reload=, mcp=, run=, log=)` 는 `RegisterResult` — `StepResult` 에 `repo`(저장한 저장소 이름. 저장한 뒤 실패해도 남는다) · `project`(넣은 행)를 더한 것 — 를 돌려준다. 순서는 검사 → 저장소 등록과 재적재 → git → 프로젝트 행이다. `local_file`(저장소를 적을 설정 파일)과 `reload`(서버가 설정을 다시 읽게 하고 그 `RepoRegistry` 를 돌려주는 함수)는 기본값이 없는 인자다 — 이 모듈은 이 저장소의 설정 파일을 이름으로 알지 않는다. 서버의 `ROOT/relay.config.local.yaml` 과 `server._reload_repos` 를 넘기는 것은 M-8 이다.
  - 검사는 아무것도 만들지 않는다: 폴더에 `.uproject` 가 있어야 하고, 같은 이름으로 다른 경로가 등록돼 있거나 같은 경로가 다른 이름으로 등록돼 있으면 실패한다(있던 항목을 덮어쓰지 않는다. 같은 이름 · 같은 경로는 다시 돌린 것으로 보고 값을 맞춘다). 경로 · 이름 · 저장소가 이미 있는 프로젝트(보관된 것 포함)와 겹치는지는 `importer.conflict` 로 본다 — `POST /studio/projects` 와 같은 규칙 · 문구다. 폴더에서 읽은 MCP 포트를 보관 안 된 다른 프로젝트가 쓰고 있어도 실패한다 (가져오기는 포트를 `null` 로 두고 등록하지만, 새로 만든 프로젝트가 남의 포트를 가진 것은 M-4 배정이 틀린 것이다).
  - 저장소 등록은 `relay repo add <이름> <경로> --workspace inplace --docs Docs` 가 쓰는 것과 같은 항목(`path` 는 `/` 구분의 절대 경로)에 `mcp` 를 더한 것이다. `mcp` 의 기본은 `[unreal]` 이고, `importer.detect` 가 폴더에서 MCP 포트를 찾았을 때만 넣는다 — 프로젝트에 자기 서버가 없는데 이름만 넣으면 다른 프로젝트의 `unreal`(MNYS 55557)에 붙을 수 있다. 쓴 뒤 `reload()` 가 돌려준 `RepoRegistry` 에 그 이름이 그 경로 · `inplace` · `Docs` · 그 `mcp` 로 있고 `match(경로)` 가 그 저장소여야 한다 (§3.3 — 행을 넣기 전에. §11.1 — 서버를 다시 띄우지 않고). 아니면 실패다.
  - git: 폴더에 `.git` 이 없으면 `git init` 을 돌리고(작업 폴더는 프로젝트 폴더), `.gitignore` · `.gitattributes` 는 없을 때만 `relay_agent/studio/defaults/gitignore` · `gitattributes` 를 그대로 복사한다. 있던 `.git` 과 두 파일은 건드리지 않는다. `.mcp.json` 은 이 PC 의 절대 경로와 포트가 들어 있어 ignore 한다 (M-4 가 남긴 결정). 첫 커밋은 만들지 않고 Git LFS 도 쓰지 않는다. 두 기본본은 일반 UE 규칙으로 쓴 잠정본이다 — MNYS 의 것을 읽지 못했다 (VERIFICATION.md). `created` 는 `.git` 과 새로 쓴 두 파일이다.
  - 프로젝트 행은 `name` · `repo` 가 프로젝트 이름, `path` 가 심볼릭 링크 등을 푼 절대 경로, `mcp_port` 가 폴더에서 읽은 포트, `engine_version` 이 `.uproject` 의 `EngineAssociation`(버전 꼴이 아니면 요청의 엔진 버전)이고 `project.updated` 를 같은 트랜잭션에서 낸다. git 이 실패하면 행을 넣지 않는다 — 그때 `repo` 는 채워져 있으므로 M-8 의 정리가 `save_local_repo(remove=True)` 로 되돌리거나, 단계를 다시 돌린다.
  - 규칙 파일 판정은 `importer.rules` 하나다. `harness` 단계와 `detected.harness.claude_md` 가 같은 함수를 써서, `bootstrap.bat` 이 `CLAUDE.local.md` 만 만들어도 가져오기에서 규칙 파일이 있는 것으로 본다. 남은 어긋남: 이 단계는 `Docs` 폴더를 요구하지 않는데 `detected.harness.docs` 는 요구한다 — `bootstrap.bat` 이 `Docs` 를 만들지 않으면 그 폴더를 가져올 때 `missing` 에 `harness` 가 나온다 (bat 이 만드는지는 확인하지 못했다).
  - `reload` 가 내는 예외는 종류를 가리지 않고 단계 실패가 된다 (`error` 에 예외 이름을 적는다. 설정 항목이 틀려 나는 `TypeError` 도 같다). `RepoRegistry` 꼴(`repos` · `match`)이 아닌 것을 돌려줘도 실패다 — `server._reload_repos` 는 `None` 을 돌려주므로 M-8 은 `engine.repos` 를 돌려주는 함수를 넘긴다. 이 모듈 자체의 버그는 잡지 않으므로 단계 호출 전체를 감싸는 것은 M-8 몫이다.
  - 하네스 저장소 경로에 공백이 있어도 bat 은 그대로 실행된다 (테스트가 실제로 돌려 본다). `&` · `^` · `%` 가 든 경로는 돌려 보지 않았다.
  - 첫 커밋이 없는 동안 그 저장소의 런은 git 기준이 아니다: `journal.detect_vcs` 는 HEAD 가 없으면 `NoVcs` 로 본다. 검수 통과의 커밋(`review.commit`)이 첫 커밋이 되고 그 뒤로 `GitVcs` 다. 그 커밋이 실패하면(`user.name` 없음 등) 스테이징을 비우고 파일은 그대로 둔다. `.mcp.json` · `Saved/` 는 기본 `.gitignore` 로 첫 커밋에 들어가지 않는다.
  - `mcp: [unreal]` 이 가리키는 서버는 이 순서로 먼저 있는 것이다: `~/.claude.json` 의 그 폴더 항목(`projects[<경로>].mcpServers`) → 프로젝트 `.mcp.json` → `relay.config` 의 `mcp_registry` → 전역(사용자) 설정. `mcp_registry` 나 전역에 같은 이름이 있어도 프로젝트 `.mcp.json` 의 것이 쓰이고, 그 폴더 항목에 `unreal` 이 있을 때만 그것이 이긴다 (tests/test_mcp_attach.py).

**파트 · 직원**

| 메서드 · 경로 | 요청 | 응답 | 오류 | 작업 |
|---|---|---|---|---|
| `GET /studio/departments` | — | 행 목록 | | S-3 |
| `POST /studio/departments` | `{name, default_relay, default_mcp, default_models, guide_template}` (`name` 만 필수) | 201 행 (파트방도 만든다, §3.6) | `conflict` (이름) · `missing_fields` (빈 이름) · `precondition_failed` (기본값이 §9.4 에 걸린다, §3.4) | H-1 |
| `PATCH /studio/departments/{id}` | 위 필드 중 일부 | 행 (이름이 바뀌면 파트방 제목도 바뀐다) | `not_found` · `conflict` · `missing_fields` · `precondition_failed` (위와 같다) | H-1 |
| `POST /studio/departments/{id}/archive` | — | 행 (파트방도 보관한다. 이미 보관됐으면 같은 행) | `not_found` · `precondition_failed` (`builtin` 이거나 재직 직원이 있다) | H-1 |
| `GET /studio/employees` | `?project_id` · `?department_id` · `?retired=0·1` | 행 목록 + 각 `dm_room_id` · `company_wide` | | S-3 |
| `GET /studio/employees/{id}` | — | 행 + `{dm_room_id, company_wide, files, tasks[], runs[]}` | | S-3 |
| `PATCH /studio/employees/{id}` | `{name}` | 행 | `not_found` · `missing_fields` · `conflict` | H-3 |
| `PUT /studio/employees/{id}/files/{name}` | `{text, base_sha256}`. `name` 은 `PERSONA.md` · `GUIDE.md` · `MEMORY.md` · `employee.yaml` | `{name, sha256, commit}` (`commit` 은 커밋하지 못했으면 `null`) | `not_found` · `precondition_failed` | H-3, H-4 |
| `GET /studio/employees/{id}/history` | `?file` | `[{commit, at, message, files[]}]` (배열 그대로) | `not_found` · `engine_error` | H-3 |
| `POST /studio/employees/{id}/revert` | `{commit}` | `{commit}` (되돌린 뒤의 새 커밋) | `not_found` · `precondition_failed` · `engine_error` | H-3 |
| `POST /studio/employees/{id}/assign` | `{project_id}` (NULL = 해제) | 행 | `not_found` · `precondition_failed` | H-3 |
| `POST /studio/employees/{id}/leave` | `{on}` | 행 | `not_found` · `precondition_failed` | H-3 |
| `GET /studio/employees/{id}/memory` | — | `{items[], bytes, limit, sha256, commit}` | `not_found` | H-4 |
| `PATCH /studio/employees/{id}/memory/{item_id}` | `{text}` | 위와 같다 (`commit` 은 이 변경의 커밋) | `not_found` · `missing_fields` · `precondition_failed` (`item_unknown` · `memory_too_large`) | H-4 |
| `DELETE /studio/employees/{id}/memory/{item_id}` | — | 위와 같다 | `not_found` · `precondition_failed` (`item_unknown`) | H-4 |
| `POST /studio/employees/{id}/retire` | — | 행 | `not_found` · `precondition_failed` | H-3 |

- 직원을 만드는 엔드포인트는 없다. 직원은 `hire` 결재의 승인(§5.5)으로만 생긴다. 예외는 서버가 시작할 때 한 번씩 만드는 둘이다 (§3.15): 직원이 한 명도 없는 DB 의 검증용 시드 직원, 그리고 `hr` 파트의 직원 행이 없는 DB 의 기본 인사 직원(H-2).
- 직원 행의 `company_wide` 는 파트가 `hr` 이면 `true` 다 — 프로젝트가 아니라 회사 소속이라는 표시다 (§3.5). `GET /studio/employees?project_id=…` 는 그 프로젝트에 배치된 직원 뒤에 재직 중인 인사를 함께 싣는다(인사의 `project_id` 는 NULL 그대로다). `department_id` 가 `hr` 이 아닌 값이면 인사를 넣지 않고, 퇴사한 인사는 `retired=1` 이어도 넣지 않는다. 인사가 아닌 미배치 직원은 나오지 않는다. `project_id` 가 없는 조회는 전과 같다. 없는 프로젝트의 `project_id` 면 오류가 아니라 빈 목록이고 인사도 싣지 않는다.
- `files` 는 `{persona_md, guide_md, memory_md, employee_yaml}` 이다. 값은 그 파일의 본문이고 파일이 없으면 `null` 이다. `tasks[]` 는 그 직원이 담당이고 종결(`done` · `rejected` · `cancelled`)이 아닌 작업 행이다 — 끝난 작업까지는 `GET /studio/tasks?assignee_id` 로 본다 (S-3). `runs` 는 그 직원의 지금 도는 studio 런 `[{run_id, room_id, task_id, mode}]` 이다.
- 파일 · 이력 · 되돌리기 경로는 H-3 확정이다 (`studio/api.py` · `studio/employee_git.py`. 순서와 `failed[]` 코드는 §9.6). `name` 과 `?file` 은 네 파일 이름만 받고 그 밖의 값은 422 다. `PUT` 의 `base_sha256` 은 고치기 전에 읽은 파일의 SHA-256 이고 파일이 없었으면 빈 문자열이다. 보낸 본문이 지금 파일과 같으면 쓰지 않고 지금의 마지막 커밋을 `commit` 으로 돌려준다. `history` 는 감싸지 않은 배열이다 (`GET /studio/models` 와 같다): 최신 먼저 최대 200건, `at` 은 §3.1 의 지역 시각, `files[]` 는 그 커밋이 바꾼 파일 전부이고, `?file` 을 주면 그 파일을 바꾼 커밋만 싣는다. 이력이 없는 폴더는 빈 배열이다. `revert` 의 `commit` 은 이력에 있는 커밋의 sha 또는 그 앞 7–40자다.
- 이름 변경 · 배치 · 휴직 · 퇴사 경로는 H-3 확정이다 (`studio/api.py`). 넷 모두 읽기 · 검사 · 쓰기 · 이벤트가 한 트랜잭션이고, 값이 그대로면 쓰지도 이벤트를 내지도 않고 지금의 행을 돌려준다. 응답은 `dm_room_id` · `company_wide` 가 붙은 직원 행이다. 바뀌었으면 커밋 뒤에 `running` 캠페인을 모두 훑는다 (§4.10).
- `PATCH` 는 `name` 을 앞뒤 공백을 떼어 받고 비면 422 `missing_fields`(`fields=["name"]`)다. 퇴사한 직원의 이름과 겹쳐도 409 `conflict`(`field="name"`)다 — 이름은 퇴사자까지 유일하다 (§3.5). 퇴사한 직원도 이름을 바꿀 수 있다(이름을 비우는 길이다). 1:1 방 제목이 옛 이름과 같으면 새 이름으로 바꾸고 `room.updated` 를 낸다. `change = renamed`.
- `assign` 의 `project_id` 는 키가 필수이고 `null` 이면 해제다. `precondition_failed` 의 `failed[]` 는 `retired`(퇴사한 직원) · `company_wide`(인사에게 프로젝트를 준다 — 인사는 `null` 만 받는다) · `project_unknown`(없는 프로젝트) · `project_archived`(보관된 프로젝트) · `active_tasks`(§4.9). `change = assigned`. 프로젝트가 같으면 아무 일도 없다. 하네스(`CLAUDE.md` 류 규칙 파일과 `Docs` 폴더, 판정은 `importer.harness_ready` 한 곳)가 깔리지 않은 프로젝트에는 프로그램 파트(`department_id = program`) 직원을 배치할 수 없다 — `precondition_failed`, `failed[]` 에 `harness_missing` (S-11 ②; `hire` 승인의 배치도 같다, §5.5). 다른 파트 · 사용자 정의 파트 · `project_id = null` 은 대상이 아니다.
- `leave` 의 `{on}` 은 `true` 면 `on_leave`(`active_tasks` 로 거절될 수 있다), `false` 면 §4.9 의 규칙으로 다시 계산한 상태다. 퇴사한 직원은 `retired` 로 거절한다. 상태가 바뀌면 `employee.status` 를 먼저, `employee.updated`(`change = leave`)를 다음에 쓴다. 이미 그 상태면 아무 일도 없다. 휴직이 막는 것은 작업 시작(§4.3 의 T3)과 제안 도구(`task_propose` · `followup_propose`, §8)뿐이고 채팅 턴과 단체방 초대(§3.8)는 그대로다 (H-3 확정, 의도한 것).
- `retire` 는 `retired_at` 만 채운다 (`change = retired`, `active_tasks` 로 거절될 수 있다). 행 · 폴더 · 1:1 방 · 방 글 · `room_members` 는 그대로이고 방도 보관하지 않으므로 `GET /studio/rooms/{id}/messages` 로 읽힌다. 기본 목록에서 빠지고 `?retired=1` 에 나온다. 이미 퇴사했으면 같은 행을 주고 이벤트는 없다. 인사도 퇴사할 수 있다.
- 메모리 항목 (H-4 확정, `studio/api.py` · `studio/memory.py`): `GET` 은 `{items[{id, text, source, date}], bytes, limit, sha256, commit}` 이다(`source` · `date` 는 출처 꼬리가 없는 줄이면 `null`). `PATCH` 의 `text` 는 공백을 한 줄로 접고 앞의 `- ` 를 떼어 받으며 비면 422 `missing_fields`(`fields=["text"]`)다. 출처 꼬리는 그대로 두고 쓴다. `PATCH` · `DELETE` 는 파일을 쓰고 `memory: edit` · `memory: delete` 로 커밋하고 `employee.updated`(`change = file`)를 낸 뒤 `GET` 과 같은 모양을 준다. 항목 id 가 없으면(그새 서버가 줄을 바꿨거나 지웠다) 409 `precondition_failed` 의 `failed[]` 는 `item_unknown` 이고, 고친 뒤의 파일이 4096바이트를 넘으면 `memory_too_large` 다. 파일이 이미 상한을 넘어 있으면(손으로 고친 경우) 파일을 키우지 않는 변경, 곧 삭제와 줄이는 수정은 넘은 채로도 받는다. id 가 줄의 내용이라 `base_sha256` 은 받지 않는다. 파일 전체를 쓰는 길은 `PUT …/files/MEMORY.md` 그대로다.

**방 · 글 · 첨부**

| 메서드 · 경로 | 요청 | 응답 | 오류 | 작업 |
|---|---|---|---|---|
| `GET /studio/rooms` | `?kind` (`dm` · `task` · `department` · `system` · `group`) · `?archived=0·1` | 행 목록 + 각 `{unread, last_message}` | | S-3 |
| `POST /studio/rooms` | `{employee_ids[], title, client_room_id}` — 단체방을 만든다. `title` · `client_room_id` 는 없어도 된다 | 201 행 + `{members[], unread}`. 시스템 글 하나와 `room.updated` · `message.created` 를 낸다. 같은 `client_room_id` 의 재시도는 200 과 그 방의 행 + `{members[], unread}` 이고 이벤트를 다시 내지 않는다 | `precondition_failed` (2명 미만 · 상한 초과 · 퇴사한 직원) · `not_found` (없는 직원) | 단체방 |
| `GET /studio/rooms/{id}` | — | 행 + `{members[], unread}` | | S-3 |
| `PATCH /studio/rooms/{id}` | `{muted}` | 행. 값이 바뀐 때만 `room.updated` 를 낸다. 음소거가 막는 것은 §5.9 | `not_found` | N-2 |
| `POST /studio/rooms/{id}/read` | `{message_id}` | 행 (`owner_read_id` 는 줄어들지 않는다) | | S-3 |
| `POST` · `DELETE /studio/rooms/{id}/members/{employee_id}` | — | `{members[]}` (지금 구성원. 나간 구성원은 뺀다). 둘 다 멱등이고 바뀐 때만 `room.updated` 를 낸다. `DELETE` 는 행을 지우지 않고 `left_at` 을 채우며, `POST` 가 그 행을 되살린다 | `precondition_failed` (작업방도 단체방도 아니다 · 작업방의 `POST`: 그 프로젝트에 배치되지 않은 직원이다 · 작업방의 `DELETE`: 그 작업의 담당이다 — 담당은 `/tasks/{id}/assign` 으로 바꾼다 · 단체방: 표 아래) | S-9, 단체방 |
| `POST /studio/rooms/{id}/rename` | `{title}` | 행. 이름이 바뀐 때만 시스템 글 하나와 `room.updated` · `message.created` 를 낸다 | `precondition_failed` (단체방이 아니다 · 보관한 방이다) · `missing_fields` (`title` 이 비었다) | 단체방 |
| `POST /studio/rooms/{id}/archive` | — | 행 (`archived_at`). owner 가 단체방에서 나간다. 처음 보관할 때만 `room.updated` 를 낸다 | `precondition_failed` (단체방이 아니다) | 단체방 |
| `POST /studio/rooms/{id}/continue` | — | 행. 왕복 한도에 멈춘 단체방 · 파트방을 이어 간다 (§4.6): `exchange_count` 를 0 으로 돌리고 시스템 글 하나와 `room.updated` · `message.created` 를 낸 뒤 세워 둔 턴을 넣는다. 멈춘 방이 아니면 아무것도 하지 않고 행을 돌려준다 | `precondition_failed` (단체방도 파트방도 아니다 · 보관한 방이다) | 단체방 · 파트방 |
| `DELETE /studio/rooms/{id}` | — | 200 `{id, deleted: true}`. 단체방(보관한 것도)을 방 행 · 글 · 구성원 행(읽음 표시 `fed_message_id` 와 대화 세션 포함) · 글의 첨부 파일째 지운다. 한 트랜잭션에서 지우고 `room.deleted` 를 낸 뒤 커밋하면 글마다 `attachments/<글 id>/` 폴더를 지운다 | `not_found` · `precondition_failed` (1:1 방 — 메시지가 `POST …/clear` 를 안내한다 · 작업방 · 파트방 · 시스템 방은 지울 수 없다 · 그 방에서 직원의 턴이 돌거나 기다린다: `failed` = `turn_running` · `turn_waiting`) | 방 삭제 (T4) |
| `POST /studio/rooms/{id}/clear` | — | 200 방 행. 1:1 방의 글과 첨부 파일만 지우고 방은 남긴다: `owner_read_id` 를 0 으로, 구성원 행의 `session_id` · `conversation_id` 를 `NULL` 로, `fed_message_id` 를 0 으로 돌려 직원의 대화가 처음부터 다시 시작한다. `room.cleared` 를 낸다. 비어 있는 방에 해도 같다 | `not_found` · `precondition_failed` (1:1 방이 아니다 — 단체방은 `DELETE` 를 안내한다 · 직원의 턴이 돌거나 기다린다: `failed` 는 위와 같다) | 방 삭제 (T4) |
| `GET /studio/rooms/{id}/messages` | `?before_id` · `?after_id` · `?limit` (기본 50, 최대 200) | `{items, has_more}` (`id` 오름차순) | | S-3 |
| `POST /studio/rooms/{id}/messages` | `{body, mentions[], attachments[], client_msg_id}` | 201 글 행 | `missing_fields` · `precondition_failed` (보관한 단체방도 여기에 든다, §6.6) | S-3, S-7, S-9. 알림 답장(N-3)이 그대로 쓴다: 기기 토큰으로 `{body, client_msg_id}` |
| `POST /studio/attachments` | `{name, data_b64}` | 201 `{id, name, mime, size}` | `payload_too_large` | S-3 |
| `GET /studio/attachments/{id}/{name}` | — | 파일. `id` 는 글 id, 결재 id 또는 방에 올리지 않은 스크린샷의 `shot_<hex12>` 다 (§6.6) | | S-3 |
| `GET /studio/app/latest` | `?abi` (앱이 `Build.SUPPORTED_ABIS[0]` 을 보낸다. 없으면 `arm64-v8a`) | 있으면 `{available: true, version_name, build_number, size, sha256, uploaded_at, notes, abi, file, url}`, 없으면 `{available: false, reason}` (`no_folder` · `no_info` · `bad_info` · `no_apk`) — 둘 다 200. 기기 토큰 · owner 토큰 | | A-13, §12.4 #48 |
| `GET /studio/app/files/{name}` | — | APK 파일(`application/vnd.android.package-archive`). `name` 은 위 `file` 이다. 폴더 밖을 가리키는 이름(`..` · 경로 · 링크) · `.apk` 가 아닌 이름 · 없는 파일은 이유를 가리지 않고 404 | `not_found` | A-13, §12.4 #48 |

- 방 지우기 · 대화 지우기 (T4): owner 토큰과 기기 토큰만 부를 수 있다 — 행위자 토큰은 `/studio/mcp/` · `/studio/guard/` 밖이라 403 `actor_not_allowed` 다 (§6.2). 지우는 것은 방 · 글 · 구성원 행 · 글의 첨부 파일뿐이다. 결재함(`inbox`) · 작업 · 직원 메모리(`MEMORY.md`)는 그대로 남고, 지운 방을 가리켜도 목록과 상세는 실패하지 않는다. 확인 순서: 방(404) → 방 종류 → 도는 턴(`turn_running` = 그 방에서 직원의 턴이 돈다, `turn_waiting` = 단체방의 줄에 직원이 기다린다; 둘 다면 둘 다 담는다). 지운 방의 `stream_events` 는 그대로 둔다.
- `unread` 는 `id > owner_read_id` 이고 owner 가 쓰지 않은 글의 수다. `last_message` 는 그 방의 마지막 글 행이고 글이 없으면 `null` 이다. `read` 의 `message_id` 가 그 방의 마지막 글 id 보다 크면 마지막 글 id 로 맞추고, 값이 실제로 늘 때만 `room.updated` 를 낸다 (S-3).
- 글 목록은 `before_id` 만 주면 그보다 앞의 최근 `limit` 개, `after_id` 만 주면 그보다 뒤의 처음 `limit` 개, 둘 다 없으면 가장 최근 `limit` 개다 (§3.8 의 `(room_id, id)` 인덱스). 둘 다 주면 그 사이(`after_id < id < before_id`)의 최근 `limit` 개다 (잠정, S-3). 저장소의 `list_messages` 인자가 같은 뜻이다. `has_more` 는 읽은 방향으로 `limit` 개 밖에 글이 더 있다는 뜻이다. `limit` 이 200 을 넘으면 줄여서 받지 않고 422(검증 오류)다 (S-3).
- owner 가 글을 쓴 뒤의 일 (§6.6).
- 단체방 만들기 (§3.6): `employee_ids` 는 겹친 id 를 하나로 센 뒤 2명 이상이고 `Config.studio_group_max_members`(기본 8) 이하여야 한다. 초대할 수 있는 직원은 재직 중인 직원 누구나다 — 인사도, 프로젝트에 배치되지 않은 직원도 된다. 퇴사한 직원(`retired_at`)은 안 되고, 휴직은 아직 가리지 않는다. 보는 순서는 `client_room_id` → 수(2명 미만 → 상한 초과) → 없는 직원 → 퇴사한 직원이고, 하나라도 걸리면 아무것도 만들지 않는다. 없는 직원은 전원을 먼저 본다 — 퇴사한 직원과 없는 id 를 함께 주면 순서와 상관없이 `not_found` 다. `title` 은 앞뒤 공백을 떼고, 비었으면 구성원 이름을 받은 순서대로 `, ` 로 잇는다. `{title}` · `{employee_ids}` · `{client_room_id}` 밖의 키는 422 다. 앱은 상한을 `GET /studio/machine` 의 `limits.group_max_members` 로 미리 안다.
- `client_room_id` 는 중복 방지용이다 (글의 `client_msg_id` 와 같은 쓰임, §6.6). 방을 만들 때 쓰는 시스템 글의 `meta.client_room_id` 에 넣고 (§7.6), 보관한 방을 포함한 단체방 전부의 첫 글에서 찾는다. 같은 값으로 만든 방이 있으면 새로 만들지 않고 200 과 그 방을 돌려준다 — 본문은 `GET /studio/rooms/{id}` 와 같고, 시스템 글도 이벤트도 다시 내지 않는다. 이때 요청의 나머지(`employee_ids` · `title`)는 보지 않는다: 그 사이 구성원이 퇴사했거나 방을 보관했어도 200 이다. 빈 문자열과 `null` 은 키가 없는 것이고, 키가 없으면 요청마다 새 방이다. 방마다 첫 글을 하나씩 읽으므로, 단체방이 아주 많아지면 `rooms` 의 칸으로 옮긴다 (그때는 마이그레이션이다, §3.2).
- 단체방의 구성원은 작업방과 같은 경로로 넣고 뺀다. `POST` 는 재직 중인 직원만 받고 프로젝트는 보지 않는다. 지금 구성원이 상한만큼이면 `precondition_failed` 다 — 이미 앉아 있는 직원은 상한을 묻지 않고(아무것도 바뀌지 않는다), 나갔다 돌아오는 직원은 묻는다. 돌아온 직원은 나가기 전의 `room_members` 행을 그대로 잇는다 (§3.7). `DELETE` 는 구성원 누구든 뺀다 (담당이 없다. 남는 수의 아래 한도도 없다). 보관한 단체방에는 둘 다 `precondition_failed` 다. owner 의 글도 받지 않는다 (§6.6).
- `rename` 은 `title` 의 앞뒤 공백을 떼고, 지금 이름과 같으면 아무것도 하지 않고 행을 돌려준다. `archive` 는 이미 보관한 방이면 같은 행을 돌려주고 이벤트를 다시 내지 않는다. 보관은 방에 시스템 글을 쓰지 않고 구성원 행도 건드리지 않는다.
- 단체방을 만들 때 · 초대 · 내보내기 · 이름 변경은 그 방에 시스템 글을 하나 쓰고 (`meta.event` 는 차례로 `group_invited` · `group_invited` · `group_removed` · `group_renamed`, §7.6) `room.updated` 다음에 `message.created` 를 낸다. owner 가 한 일이므로, 그 글을 쓰기 전에 안 읽은 글이 없었으면 `owner_read_id` 를 그 글로 올려 `unread` 가 늘지 않는다. 안 읽은 글이 있었으면 `owner_read_id` 를 그대로 둔다. 이 글은 푸시하지 않는다 (§5.9 의 즉시 알리는 종류가 아니다).
- `continue`(이어 가기)는 멈춘 단체방 · 파트방에서만 일한다 (§4.6). 보는 순서는 없는 방(`not_found`) → 단체방도 파트방도 아니다 → 보관한 방이다 → 멈춘 방인가다. 멈춘 방이면 `exchange_count` 를 0 으로 돌리고 시스템 글(`meta.event = group_continued`. 이것도 푸시하지 않는다)을 쓴 뒤, 세워 둔 턴을 방의 순서(§6.6)로 넣는다 — 응답은 턴을 기다리지 않는다. 멈춘 방이 아니면 200 과 행뿐이고 글도 이벤트도 턴도 없다: 여러 번 눌러도 글이 늘지 않는다. 앱은 한도를 `GET /studio/machine` 의 `limits.group_max_exchanges` 로 안다.

**캠페인 · 작업**

| 메서드 · 경로 | 요청 | 응답 | 오류 | 작업 |
|---|---|---|---|---|
| `GET /studio/campaigns` | `?project_id` · `?status` | 행 목록 + 각 `progress` | | S-3, S-13 |
| `POST /studio/campaigns` | `{project_id, title, goal, mode}` — `mode` 가 없으면 `milestone` | 201 행 (`draft`) | `missing_fields` · `precondition_failed` (없는 프로젝트) | S-14 |
| `GET /studio/campaigns/{id}` | — | 행 + `{progress, tasks[]}` | | S-13 |
| `PATCH /studio/campaigns/{id}` | `{title, goal, mode}` 중 일부. null 과 빈 값은 받지 않는다. 달라진 것이 없으면 쓰지 않고 행을 그대로 준다 | 행 | `missing_fields` · `precondition_failed` (`done` · `cancelled` 캠페인) | S-14 |
| `POST /studio/campaigns/{id}/{action}` | `action` 은 `start`(`draft` 에서) · `pause`(`running`) · `resume`(`paused`) · `cancel`(끝나기 전) (§4.10). 본문 없음. 그 밖의 `action` 은 422 | 행 | `illegal_transition` · `stale_state` | S-14 |
| `GET /studio/tasks` | `?project_id` · `?campaign_id` · `?status` · `?assignee_id` · `?parent_id` | 행 목록 | | S-3 |
| `POST /studio/tasks` | C2: `{project_id, title, reason, spec, done_criteria, type, campaign_id, parent_id, assignee_id, is_milestone, relay, depends_on[]}` (앞의 여섯이 필수) | 201 행 | `missing_fields` · `precondition_failed` | S-2, S-3 |
| `GET /studio/tasks/{id}` | — | 행 + `{room_id, depends_on[], dependents[], deps_met, pending_inbox_id, queue_position, run}` | | S-3 |
| `PATCH /studio/tasks/{id}` | `{title, reason, spec, done_criteria, type, relay, campaign_id}` 는 `backlog` 에서만. `{is_milestone, sort_order}` 는 종결 전이면 된다 | 행 | `precondition_failed` · `missing_fields` | S-3 |
| `POST /studio/tasks/{id}/assign` | `{assignee_id}` (NULL = 해제). `backlog` 에서만 (§3.10) | 행 | `precondition_failed` | S-3, A-8 |
| `PUT /studio/tasks/{id}/deps` | `{depends_on[]}`. `proposed` · `backlog` 에서만 (§3.11) | 행 + `depends_on[]` | `precondition_failed` (순환, 다른 프로젝트) | S-3 |
| `POST /studio/tasks/{id}/start` | `{stage_models}` (선택. 이번만 덮어쓰기) | 행 (`queued_at` 이 찬 `backlog`) | `illegal_transition` · `precondition_failed` | S-8 |
| `POST /studio/tasks/{id}/stop` | — | 행 | `precondition_failed` | S-8, A-8 |
| `POST /studio/tasks/{id}/cancel` | `{reason}` (선택) | 행 (`cancelled`) | `illegal_transition` · `precondition_failed` · `stale_state` · `engine_error` | S-2, S-3 |
| `GET /studio/tasks/{id}/events` | `?after_id` · `?limit` | `{items, has_more}` | | S-3 |

- `progress` 는 `{by_status: {…}, total, done_ratio}` 다 (S-13). `run` 은 `{id, status, changes_status}` 또는 NULL 이다.
- `by_status` 는 §4.1 의 여덟 상태가 모두 키이고 없는 상태는 0 이다. `total` 은 그 캠페인의 작업 수다. `done_ratio` 는 `done / (total − rejected − cancelled)`(0–1)이다 — 취소 · 반려된 작업은 남은 일이 아니라 분모에서 빼고, `total` 과 `by_status` 에는 그대로 둔다. 분모가 0 이면(작업이 없거나 모두 취소 · 반려) 0 이다 (S-13 확정, `studio/stats.py` 의 `progress`). `run` 은 그 작업의 `run_id` 로 엔진에서 읽은 `RunState` 의 세 값이다 — 런이 없거나(`run_id` 가 NULL) 엔진이 그 런을 읽지 못하면 NULL 이다. `server.py` 가 `api.use_engine` 으로 엔진을 넘기고 `api._run_view` 가 `studio/review.py` 의 `run_view` 로 읽는다 (S-12 확정).
- 목록의 `?status` 는 값 하나다. 캠페인은 §4.10, 작업은 §4.1 의 이름이고 그 밖의 값은 422(검증 오류)다. 작업 목록의 순서는 `sort_order`, 그다음 만든 순서다 (S-3).
- `POST /studio/tasks` 는 필수 여섯 중 빈 것을 모두 모아 `missing_fields` 의 `fields[]` 에 싣는다. 없는 프로젝트 · 부모 · 캠페인 · 선행 작업, 프로젝트가 다른 캠페인 · 선행 작업, 재직 중이 아닌 담당은 `precondition_failed` 이고 `failed[]` 는 빈 배열이다 (§6.3). `depends_on[]` 은 작업을 만드는 트랜잭션에서 함께 넣는다 (§3.11. S-3).
- `GET /studio/tasks/{id}` 가 덧붙이는 것: `room_id` 는 작업방 id, `depends_on[]` · `dependents[]` 는 작업 id 목록, `deps_met` 은 §4.6 의 "선행 조건" 판정, `pending_inbox_id` 는 그 작업의 `pending` 결재 id 다 (없으면 NULL). `queue_position` 은 서버 전체 줄(`queued_at` 오름차순)에서 1부터 센 자리이고 줄에 없으면 NULL 이다 (S-3. S-8 확정 — 프로젝트별로 세지 않고 서버 전체 줄 그대로다).
- `PATCH /studio/tasks/{id}` 는 보낸 키만 바꾼다. 줄 서 있는(`queued_at` 이 찬) 작업은 `backlog` 여도 앞의 일곱 필드를 고칠 수 없다 — 시작 요청이 검사한 내용이 그대로여야 한다. `stop` 으로 줄에서 뺀 뒤 고친다 (잠정, S-3). NULL 은 `relay` · `campaign_id` 만 받고(비운다는 뜻) 나머지에 NULL 이나 빈 글이 오면 `missing_fields` 다. 바뀐 값이 없으면 쓰지 않고 지금 행을 준다. 바꾸면 `task_events` 에 `edited` 행이 남는다 (`detail.fields` 에 필드별 `{from, to}`). `is_milestone` 은 `from` · `to` 모두 true/false 이고, T1 승인 수정이 남기는 `edited` 행도 같다. 담당은 `assign`, 선행 관계는 `deps` 경로로만 바꾼다 (S-3).
- 글 필드 네 개(`title` · `reason` · `spec` · `done_criteria`)는 등록 · 수정 모두 받은 그대로 저장한다. 앞뒤 공백을 자르지 않고, 공백만 다른 글도 다른 값이다. 공백뿐이면 빈 글이다 (§3.10 의 CHECK). `relay` · `campaign_id` · `parent_id` · `assignee_id` 는 앞뒤 공백을 자르고 빈 글은 NULL 이다 (S-3).
- `PUT /studio/tasks/{id}/deps` 는 선행 집합을 통째로 바꾼다. 자기 자신 · 없는 작업 · 다른 프로젝트의 작업 · 순환은 `precondition_failed` 이고 `failed[]` 는 빈 배열이다. 줄 서 있는 작업도 같다 (잠정, S-3). 집합이 같으면 쓰지 않는다. 바꾸면 `deps` 행이 남는다 (`detail` 은 `{from[], to[]}`).
- `POST /studio/tasks/{id}/cancel` 의 `engine_error` 는 쓰기 전에 엔진이 거부한 경우뿐이다 — 작업과 결재는 그대로다. 커밋 뒤의 후속 처리가 실패하면 전이는 유지되고 서버 오류(500)로 나간다. 다시 읽으면 작업은 `cancelled` 다 (S-3). S-12 확정: 조건을 못 채운 취소 — 런이 아직 돈다, 되돌릴 수 없다 — 는 `precondition_failed` 와 §4.3 의 `failed[]` 코드다. 런이 엔진에서 사라진 작업의 취소는 되돌리지 않고 받는다 (§4.3 의 `failed[]` 표 다음). `engine_error` 는 런 정리(`cancel`)나 되돌리기(`rollback_changes` · `discard_changes`)를 엔진이 `ValueError` 로 거부한 경우다. 커밋 뒤의 후속 처리(`Effects.after`)는 T10 · T11 에서 하는 일이 없다 — 줄을 서는 전이가 아니고 런도 만들지 않는다. 그래서 후속 처리의 오류 종류를 따로 두지 않고, 500 분기는 그 가정이 깨질 때의 방어로만 남긴다.
- `GET /studio/tasks/{id}/events` 는 `id` 오름차순이고 `after_id` 뒤의 처음 `limit` 개다 (기본 50, 최대 200 — 글 목록과 같다. S-3).
- `start` 는 T3 의 계기다 (§4.3). 전이는 슬롯을 얻은 때 일어나므로 응답의 작업은 아직 `backlog` 다. `stage_models` 는 `tasks.stage_models_json` 에 저장하고, 상한(`model_cap` · `effort_cap`)을 넘으면 `precondition_failed` 다 (§11.2). 상한이 어디서 오는지, 검사 순서, `failed[]` 의 코드는 §9.5 다.
- `stop` 은 전이가 아니다. 줄 서 있는 `backlog` 작업은 줄에서 뺀다(`queued_at = NULL`, `unqueued`). 줄 서 있는 `working` 작업(T5 · T8 뒤)도 먼저 줄에서 뺀다(`queued_at = NULL`, `unqueued`) — 런이 이미 멈춰 있으면 그 자리에서 system 이 T4(`question`, `run_cancelled`)를 하고 런은 멈춰 있던 상태 그대로 둔다. 런이 아직 돌면 줄에서 뺀 뒤 `RelayEngine.cancel` 로 끊는다. 줄에서 먼저 빼므로 진행 스레드가 돌아올 때 §4.7 표의 `cancelled` · `working` 행이 받는다 (S-8 확정). 런이 도는 작업은 `RelayEngine.cancel` 로 끊고, 진행 스레드가 돌아오면 §4.7 표의 `cancelled` 행이 T4(`question`, `run_cancelled`)를 한다. `stop` 은 작업을 종결하지 않는다 — 그만두려면 `blocked` 가 된 뒤 `cancel` 을 부른다(T11). 그래서 "중지"의 끝은 `blocked` 다. `[§12]`
- `cancel` 은 작업 취소다 — `backlog` 면 T10, `blocked` 면 T11 (§4.3). owner 만 부른다. `reason` 은 `task_events.reason` 에 남는다. 그 밖의 상태는 `illegal_transition` 이다 — `proposed` · `working` · `review` 는 F10, `done` · `rejected` 는 F9, 이미 `cancelled` 면 F0 이다 (§4.4 의 표). `working` 은 `stop` 을 먼저 불러 `blocked` 가 된 뒤, `review` 는 결재의 되돌리기(T9)로 `backlog` 가 된 뒤 부른다. `blocked` 인데 런이 아직 돌거나 변경을 되돌릴 수 없으면 `precondition_failed` 다. 이름이 같은 것과 구분한다 — 캠페인의 `cancel`(위 표, §4.10)은 작업을 취소하지 않고, 런을 끊는 `RelayEngine.cancel` 은 `stop` 이 부르며, 결재의 `cancelled` 는 T11 이 차단 결재를 닫을 때만 따라온다 (§5.1).
- S-3 의 "tasks(+전이 액션)"에 해당하는 것은 `start` · `stop` · `cancel` 과 결재의 `decide` 다. T1 · T2 · T5 · T7 · T8 · T9 를 직접 부르는 경로는 없다 — 결재 결정으로만 일어난다. owner 가 경로로 직접 부르는 전이는 취소(T10 · T11)뿐이다 (§4.2 의 "통로").

**결재 · 집계**

| 메서드 · 경로 | 요청 | 응답 | 오류 | 작업 |
|---|---|---|---|---|
| `GET /studio/inbox` | `?status` (기본 `pending`) · `?kind` · `?project_id` · `?task_id` · `?before` · `?limit` | `{items, has_more, pending_count}` | | S-5 |
| `GET /studio/inbox/{id}` | — | 행 + `{task, allowed_decisions[]}`. `final_review` 면 `package` (§5.4) | | S-5, S-12 |
| `POST /studio/inbox/{id}/decide` | `{decision, comment, data, via}` (§5.1) | `{inbox, task}` | `already_decided` · `invalid_decision` · `precondition_failed` · `stale_state` · `conflict` · `engine_error` | S-5. 알림 승인(N-3)이 그대로 쓴다: 기기 토큰, `via=notification` (§5.9) |
| `GET /studio/stats` | `?project_id` · `?campaign_id` · `?from` · `?to` | `{campaigns[], employees[], inbox}` | | S-13 |
| `GET /studio/growth` | — | `{studio, employees[], achievements[], today}` | | S-13 |

- `pending` 목록은 `created_at` 오름차순(오래 기다린 것부터), 그 밖의 상태는 `closed_at` 내림차순이고 `before` 는 `closed_at` 커서다. 잠정 (S-5): 같은 초에 닫힌 결재는 한 페이지에 함께 준다 — 그때 `items` 는 `limit` 보다 길 수 있다. `pending` 목록은 커서 없이 `limit` 개까지 주고, 더 있는지는 `has_more` 와 `pending_count` 로 안다.
- `stats` (S-13 확정, `studio/stats.py` 의 `collect`). 저장하지 않고 요청마다 `tasks` · `task_events` · `inbox` 와 런의 비용에서 센다. 런의 비용은 캐시하지 않는다 — 요청 하나가 필터와 기간에 드는 런의 상태를 런마다 한 번 읽는다(`review.run_cost`). 런은 멈췄다가 같은 런으로 이어 돌고(T5 · T8) `done` 으로 끝난 뒤에도 다시 열려(§4.7 의 `done` 행) 비용이 더 붙을 수 있어서, 읽어 둔 값을 그대로 쓸 수 없다. 런이 많아 느리면 `from` · `to` 로 기간을 좁힌다.
  - `campaigns[]` 는 `{id, progress}` — 필터에 맞는 캠페인의 지금 상태이고 기간과 무관하다.
  - `employees[]` 는 `{id, tasks_done, cost_usd}` — 재직 직원(`project_id` 를 주면 그 프로젝트의 직원과 프로젝트가 없는 직원)과 값이 있는 그 밖의 직원이고, 만든 순서다. `tasks_done` 은 기간 안에 `done` 이 된 작업 수다: `task_events` 의 `to_status = 'done'` 행의 `at` 으로 세고, 그 작업의 지금 담당(`tasks.assignee_id`) 몫이다. `cost_usd` 는 기간 안에 시작한 작업 런(T3 이벤트의 `detail.run_id`)의 `RunState.cost_usd` 합(소수 4자리)이다 — 런은 시작한 시각이 든 기간에 통째로 세고, 엔진이 읽지 못하는 런은 0 이다. 채팅 · 읽기 턴의 비용은 들지 않는다. `cost_usd` 도 `tasks_done` 처럼 작업의 지금 담당 몫이다 — T3 행은 그때의 담당을 남기지 않으므로(`detail` 은 `run_id` 뿐이다) 담당이 바뀐 작업은 옛 런의 비용도 지금 담당에게 간다. 담당이 없는 작업의 런은 세지 않는다.
  - `inbox` 는 `{pending, avg_wait_seconds}` — `pending` 은 지금 대기 중인 결재 수(기간과 무관), `avg_wait_seconds` 는 기간 안에 결정된(`closed_at` 기준) 결재의 `closed_at − created_at` 평균(초, 소수 1자리)이고 없으면 NULL 이다. 취소된 결재는 세지 않는다. `campaign_id` 를 주면 그 캠페인의 작업에 딸린 결재만 센다.
  - `from` · `to` 는 서버 현지 시각의 `from <= 시각 < to` 다. 값은 `YYYY-MM-DD` 또는 ISO 시각이고, 날짜만 온 `to` 는 그날을 포함한다. 시간대가 붙은 값은 서버 현지 시각으로 바꾼다. 읽지 못하는 값과 `from >= to` 는 422 다.
- `growth` (S-13 확정, `studio/stats.py` 의 `growth`). 앱 홈이 쓰는 성장 수치와 오늘의 결산이다. 저장하지 않고 요청마다 `task_events` · `inbox` · `tasks` 에서 다시 센다 — 테이블을 두지 않으므로 같은 기록이면 언제 계산해도 같은 값이다. 가상 재화나 게임 규칙은 없고, 수치는 아래 세 표의 규칙으로만 나온다. 필터는 없다 — 스튜디오 전체다.
  - `studio` 는 `{level, xp, level_xp, next_level_xp, counts{passed, clean, decided}}` — `level_xp` 는 지금 레벨이 시작하는 경험치, `next_level_xp` 는 다음 레벨이 시작하는 경험치다. `counts` 는 경험치를 만든 행의 수다.
  - `employees[]` 는 `{id, level, xp, level_xp, next_level_xp, counts{passed, clean}}` — 재직 직원과 경험치가 있는 그 밖의 직원이고, 만든 순서다. 통과한 작업의 지금 담당(`tasks.assignee_id`) 몫이다 — T7 행은 그때의 담당을 남기지 않으므로 담당이 바뀐 작업의 경험치는 지금 담당에게 간다. 담당이 없는 작업은 스튜디오에만 든다. 결재의 경험치는 직원에게 가지 않는다.
  - `achievements[]` 는 `{id, title, achieved, achieved_at}` — 아래 업적 표의 일곱 개가 표의 순서로 항상 온다. 달성하지 않은 업적은 `achieved = false`, `achieved_at = NULL` 이다. 한 번 달성한 업적은 뒤의 기록으로 취소되지 않는다.
  - `today` 는 `{date, from, to, done, blocked, pending}` — 오늘의 결산이다. `date` 는 서버 현지 날짜(`store.now()` 의 날짜)이고 구간은 그날의 `00:00 <= 시각 < 다음 날 00:00` 이다(`from` · `to`). `done` · `blocked` · `pending` 은 각각 `{count, items[{id, title}]}` 다. 재료는 일일 보고와 같은 함수(`stats.entries`)가 낸다 — `done` 은 오늘 `done` 이 된 작업, `blocked` 는 지금 `blocked` 인 작업, `pending` 은 지금 대기 중인 결재(오래 기다린 것부터)다. `blocked` · `pending` 은 날짜와 무관한 지금 상태다. `items` 의 `id` 는 작업 id(`done` · `blocked`)와 결재 id(`pending`)다.

경험치 규칙 (`stats.XP_PASS` · `XP_CLEAN` · `XP_DECIDED`). 통과 1건의 경험치는 재작업 없이 통과했으면 10 + 5 다:

| 기록 | 경험치 | 세는 행 | 받는 쪽 |
|---|---|---|---|
| 검수 통과 1건 | 10 | `task_events.rule = 'T7'` 인 행 | 스튜디오, 작업의 지금 담당 |
| 재작업 없이 통과 1건 | +5 | 위 행 가운데 그 작업에 `rule` 이 `T8` · `T9` 인 행이 없는 것 | 스튜디오, 작업의 지금 담당 |
| 처리한 결재 1건 | 2 | `inbox.status = 'decided'` 인 행 (종류 무관, 취소된 결재 제외) | 스튜디오 |

- 검수 통과도 결재 결정이라 통과 1건에는 결재의 2 가 함께 붙는다. 재작업(T8) · 되돌리기(T9) 결정도 처리한 결재 1건이다.
- 반려(재작업, T8) 뒤에 통과한 작업과 되돌리기(T9) 뒤에 다시 통과한 작업은 "재작업 없이 통과"에 들지 않는다 — 첫 검수에서 통과한 것이 아니다. `done` 은 마지막 상태라 한 번 정해진 값은 바뀌지 않는다.

레벨 (`stats.level_floor` · `level_of`). 레벨 `n` 이 시작하는 경험치는 `50 × n × (n − 1)` 이다. 레벨은 그 값이 경험치 이하인 가장 큰 `n` 이고 위 한계는 없다. 스튜디오와 직원이 같은 식을 쓴다:

| 레벨 | 시작 경험치 |
|---|---|
| 1 | 0 |
| 2 | 100 |
| 3 | 300 |
| 4 | 600 |
| 5 | 1000 |

업적 (`stats.ACHIEVEMENTS`). 스튜디오 단위이고, 달성 시각은 조건의 수를 채운 행의 시각이다:

| id | 이름 | 조건 | 달성 시각 |
|---|---|---|---|
| `first_pass` | 첫 통과 | 검수 통과 1건 | 1번째 T7 행의 `at` |
| `pass_10` | 통과 10건 | 검수 통과 10건 | 10번째 T7 행의 `at` |
| `pass_50` | 통과 50건 | 검수 통과 50건 | 50번째 T7 행의 `at` |
| `clean_5` | 재작업 없이 5건 | 재작업 없이 통과 5건 | 재작업 없이 통과한 5번째 T7 행의 `at` |
| `clean_20` | 재작업 없이 20건 | 재작업 없이 통과 20건 | 재작업 없이 통과한 20번째 T7 행의 `at` |
| `decide_10` | 결재 10건 | 처리한 결재 10건 | 10번째로 닫힌 결재의 `closed_at` |
| `decide_100` | 결재 100건 | 처리한 결재 100건 | 100번째로 닫힌 결재의 `closed_at` |

**런 · 사용량 (기존 조회의 별칭)**

| 메서드 · 경로 | 기존 경로 (`server.py`) | 응답 | 작업 |
|---|---|---|---|
| `GET /studio/models` | `GET /models` (291–294) | `[{provider, label, tools, models[{model, tier, exhausted_until, efforts}]}]` (`providers.py` 276–295) | S-8, A-8 |
| `GET /studio/runs/{run_id}/live` | `GET /runs/{id}/live` (719–722) | §7.4 `run.live` 의 `data` 와 같은 필드 | S-4 |
| `GET /studio/runs/{run_id}/patch` | `GET /runs/{id}/patch` (800) | 텍스트 | S-12, A-10 |
| `GET /studio/runs/{run_id}/tokens` | `GET /runs/{id}/tokens` (740) | 기존과 같다 | S-12 |
| `GET /studio/usage` | `GET /usage` (880) | 기존과 같다 | A-12 |

- 별칭은 studio 의 런(`tasks.run_id` 또는 직원의 채팅 · 읽기 턴)만 받는다. 그 밖의 런 id 는 `not_found` 다 (잠정, S-3).
- `GET /models` 는 mock 과 지금 쓸 수 없는 AI 를 뺀다 (`providers.py` 280–284). S-8 의 "지금 쓸 수 있는 모델과 그 모델이 지원하는 노력치만"이 이것이다. `/studio/models` 는 그 목록에서 가드 훅을 걸 수 있는 provider — 지금은 Claude Code — 의 항목만 돌려준다. 다른 AI 의 모델은 직원의 칸으로도 이번만 덮어쓰기로도 받지 않기 때문이다 (§9.4 의 8, §9.5 의 5, §10.8 의 9. S-10 확정 — 다른 AI 의 칸은 `provider_unguarded` 로 거부한다. 목록은 `api.list_models` 가 `work.models` 로 만든다).

**가드** (S-10)

| 메서드 · 경로 | 요청 | 응답 | 오류 | 작업 |
|---|---|---|---|---|
| `POST /studio/guard/check` | `{tool, command, rule_id}` | 200 `{decision, message, inbox_id, end_turn}` | | S-10 |

가드 훅(`studio_guard.py`, §10)이 "물어보고 진행" 목록의 명령을 잡았을 때 서버에 묻는 통로다. 훅은 DB 를 열지 않고 결재 행과 `guard` 행을 직접 만들지 않는다 — 서버가 이 요청 안에서 만든다 (§3.1, §5.1). 차단 목록의 명령은 훅이 서버에 묻지 않고 그 자리에서 거부한다 (§10). 이 경로는 S-3 의 `/studio/*` 목록에 없다. `[§12]`

- 인증: 행위자 토큰. §8.2 의 검사 1–3 을 그대로 쓰고 4(도구별 권한)는 없다. 걸리면 401 `actor_invalid`(헤더 없는 루프백 요청도 여기 든다), 맞는 owner 토큰으로 부르면 403 `actor_not_allowed` 다 (§6.2).
- `/studio/mcp/` 아래에 두지 않는 이유: 그쪽은 "도구 하나에 경로 하나, 업무 오류는 200 과 `{ok: false}`"(§8.1 · §8.3)라는 약속이 있다. 가드는 모델이 부르는 도구가 아니고 스튜디오 MCP 도구 14개(S-6 의 12개와 T5 의 `hire_recommend` · `staff_request`)에도 들지 않는다.
- 요청: `tool` 은 훅이 잡은 도구 이름, `command` 는 명령 원문, `rule_id` 는 걸린 규칙이다. 셋 다 필수이고 `risky_command` 의 payload(§5.3)에 그대로 들어간다. `tool` · `command` 의 값은 §10.2, `rule_id` 의 값은 §10.4 다.
- 응답: 판정은 모두 200 이다. `decision` 은 `allow` · `deny` 다. `message` 는 직원이 읽는 한국어 문장이고, `deny` 면 훅이 거부 이유로 모델에게 그대로 돌려준다. `inbox_id` 는 결재를 만들었을 때만 값이 있고 아니면 `null` 이다. `end_turn` 이 `true` 면 `message` 는 §8.3 의 "이제 턴을 끝내라" 문장으로 끝난다.

서버는 아래 표를 위에서부터 보고 처음 맞는 행을 따른다. "작업 런"은 토큰의 `run` 이 토큰의 `task` 의 `tasks.run_id` 와 같다는 뜻이다. 토큰의 `mode` 가 아니라 지금의 DB 로 본다 (§8.2 의 4 와 같은 원칙).

| # | 경우 | 서버가 하는 일 | 응답 |
|---|---|---|---|
| 1 | 작업 런이고, 그 작업의 그 명령에 1회 허용이 남아 있다 (`risky_command` 의 `approve`, §5.5) | 허용을 쓴다 — 한 번 쓰면 없어진다 | `allow` |
| 2 | 작업 런이고 작업이 `working` 이다 (줄 서 있어도 된다, §4.3 T4) | system 의 T4 — `risky_command` 결재를 만든다. `inbox.run_id` 는 그 런이다 | `deny` · `inbox_id` · `end_turn: true` |
| 3 | 작업 런이고 작업이 `blocked` · `review` 다 (런이 아직 도는 구간, §4.5) | 결재를 만들지 않는다. §4.5 의 표대로 `task_events` 에 `guard` 행을 남긴다 (`task.updated`, §7.5) | `deny`. `end_turn` 은 `blocked` 면 `true`(서버가 곧 그 런을 끊는다), `review` 면 `false`(런이 남은 단계를 마저 돈다) |
| 4 | 그 밖 — 작업 런이 아닌 턴(읽기 · 채팅)이거나 작업이 위 세 상태가 아니다 | 결재도 `task_events` 행도 만들지 않는다 | `deny` · `end_turn: false` |

- 2 의 뒤처리는 `ask_owner` 와 같다 — 응답을 보낸 뒤 대기 시간(`Config.studio_cut_after`, 기본 60초)이 지나도 그 런이 `running` 이면 `RelayEngine.cancel` 로 끊는다 (§8.3). 진입점은 `ask_owner` 와 같은 하나다 — `api.answered("T4", task_id, caller_run_id=그 런, inbox_id=만든 결재)`. 가드에서 이것을 부르는 곳은 `api.py` 의 `guard_check` 다 — `guard.check` 가 결재를 만들었을 때(`inbox_id` 가 있다)만 부른다 (S-10 확정). 이벤트와 푸시도 T4 그대로다 (§4.2, §5.9).
- 4 에서 결재를 만들지 않는 이유: 1회 허용은 작업에 남고 그 작업의 작업 런만 쓴다 (§5.5). 명령을 시도한 것이 작업 런이 아닌 턴이면 승인해도 그 턴이 허용을 쓸 수 없다. 직원은 거부 문장을 읽고 방에 그대로 묻는다. S-10 의 "런을 멈추고 결재함으로 보낸다"와 다른 점이다 (S-10 확정 — `guard.check`: 토큰의 작업이 없거나 그 작업의 `run_id` 가 물어 온 런이 아니면 `deny` 만 돌려준다). `[§12]`
- 훅의 실패 처리: 훅은 `decision` 이 `allow` 인 200 응답을 받았을 때만 명령을 통과시킨다. 401 · 403 · 503 · 그 밖의 HTTP 오류 · 연결 실패 · 시간 초과는 모두 거부다 (S-10 확정 — `studio_guard.judge`. 기다리는 시간은 5초다, §10.6). 열린 쪽으로 실패하면 서버가 죽었을 때 위험 명령이 그대로 실행된다. owner 확정 (§12.6 의 6): 서버에 닿지 못하면 명령을 거부한다.
- 여기서 정하지 않은 것. §10 이 정했다(10.2–10.7): 차단 목록과 "물어보고 진행" 목록, `rule_id` · `tool` 의 값, 훅이 Claude Code 에 돌려주는 응답 형식, 1회 허용의 저장 자리와 키의 구체 형식(같은 명령으로 보는 기준 포함. 키가 작업 기준이라는 것과 수명은 §5.5 가 정했다), 감사 로그. §11.4 가 정한다: 서버 주소와 행위자 토큰을 훅 프로세스에 넘기는 자리. 지금 코드의 `hook_settings`(`journal.py` 285–292)는 설정 파일에 PreToolUse 명령 줄을 argv 로만 적고, 그 설정 파일은 `JournalWorkspace` 런에만 넘어간다(`pipeline.py` 1552, `runners.py` 594–595). 가드 훅을 studio 의 런 전부에 걸려면 이 자리를 넓혀야 한다.

`/studio/mcp/*` 는 §8, `WS /studio/ws` 는 §7.

### 6.5 기기 페어링 (M-2)

1. owner 가 대시보드에서 `POST /studio/devices/pairing` 을 부른다. 이 경로는 owner 토큰이 필요하다 — 헤더 없는 요청은 루프백이어도 401 `unauthorized` 다 (§6.2 의 4). 토큰 없이 받는 것은 2 의 `/pair` 하나다. 서버가 코드(`secrets.token_urlsafe(16)`)를 만들어 메모리에 해시와 만료 시각(5분)을 둔다 (§3.14). 응답은 `{code, expires_at, qr}` 이고 `qr` 은 `{v: 1, url, code, name}` 이다 — `expires_at` 은 이 컴퓨터의 지역 시각(§6.1), `name` 은 컴퓨터 이름(`machine` 의 `name`)이다. 대시보드('📱 폰 연결')는 QR 라이브러리를 들이지 않아 지금은 주소 · 코드 · `qr` 의 JSON 을 글자로 보여 준다 (QR 로 그리는 것은 뒤 작업이고, 앱은 직접 입력이나 붙여넣기를 받는다 — A-3). `url` 은 폰이 닿을 서버 주소다. `Config` 에는 그 주소를 담은 값이 없다 — `relay remote on --tailscale` 은 `serve_host`(`0.0.0.0`) · `auth_token` · `remote_networks`(tailnet 대역)만 쓰고 주소는 저장하지 않는다 (`cli.py` 의 `remote_command`). 그래서 코드를 낼 때마다 `studio/devices.py` 의 `reach_url` 이 `relay remote status` 가 주소를 적는 순서로 고른다 (M-2 가 확정했다): (가) `serve_host` 가 루프백이면 요청이 온 주소(요청의 base URL) — 원격 접속이 꺼져 있어 폰에 알려 줄 주소가 없다. (나) Tailscale 이 연결돼 있으면 `http://<MagicDNS 이름>:<포트>`, 이름이 없으면 첫 IPv4 (`cli.py` 의 `tailscale_info`). (다) `remote_networks` 가 tailnet 전용이 아니면 이 컴퓨터의 첫 LAN IPv4 (`lan_urls`. `100.` 으로 시작하는 주소는 뺀다). (라) 그 밖에는 요청이 온 주소. 포트는 `server_url` 의 포트다 (없으면 8020). 캐시는 없다 — (나)를 보려고 코드를 낼 때마다 `tailscale status --json` 을 한 번 돌린다 (최대 10초). `relay serve --host` 로만 바깥에 연 서버는 `serve_host` 가 루프백으로 남아 (가)가 된다. MagicDNS 를 끈 tailnet 에서 폰이 그 이름을 풀지 못하는지는 실제 폰으로만 볼 수 있다 (`VERIFICATION.md`).
2. 앱이 `POST /studio/devices/pair` 로 `{code, name, platform, fcm_token}` 을 보낸다. 토큰이 없어도 받지만 §6.2 의 1(네트워크 검사)은 거친다. `name` 은 앞뒤 공백을 뺀 1–80자, `platform` 은 없으면 `android`(20자까지), `fcm_token` 은 없어도 된다 (`null` · 빈 문자열은 없는 것). 본문이 검증에 걸리면(422) 코드는 쓰이지 않고 남는다.
3. 서버가 코드를 확인하고 지운다(1회용). 기기 토큰(`sdt_` + `secrets.token_urlsafe(32)`)을 만들어 해시만 `devices` 에 넣고, 원문은 이 응답에서 한 번만 준다. 응답 `{device, token, machine}` 의 `device` 는 `token_hash` 를 뺀 기기 행이고, `machine` 은 `hello` 의 `machine` 과 같은 네 값이다 (§7.1) — 전체 진단(`GET /studio/machine`)은 몇 초 걸려 토큰 없는 경로에서 돌리지 않는다. 코드를 확인한 뒤 행을 쓰다 실패하면 그 코드는 다시 쓸 수 없다 (새 코드를 받는다).
4. 틀린 코드 · 만료된 코드 · 이미 쓴 코드는 `pairing_invalid` 다. 틀린 코드를 보내도 다른 코드는 그대로 남는다. 서버가 재시작하면 남은 코드는 모두 무효다.
5. 폐기(`revoke`)하면 `revoked_at` 을 채운다. 그 토큰은 다음 요청부터 401 이고, 그 기기의 WS 연결을 끊는다 (§7.1). 이미 폐기한 기기를 다시 폐기해도 같은 행이 온다 (`revoked_at` 은 처음 값 그대로). `GET /studio/devices` 는 `{items: [기기 행]}` 이고 폐기된 기기는 뺀다. 대시보드에는 기기 목록 · 폐기 화면이 없어 폐기는 지금 API 로만 한다 — 화면은 뒤 작업이다 (CAMPAIGN.md 의 '컴퓨터 관리(기기 폐기 포함)' 명세).

### 6.6 글 쓰기와 첨부

owner 가 `POST /studio/rooms/{id}/messages` 로 글을 쓰면 글을 저장하고 `message.created` 를 낸 뒤, 방 종류에 따라 한다.

| 방 | 하는 일 |
|---|---|
| `dm` | 그 직원의 채팅 턴을 넣는다 (S-7) |
| `task` | 작업 런이 돌고 있으면 `engine.interject` 로 끼어든다 (S-9, `server.py` 293–296 과 같은 호출). 끼어드는 것은 작업이 `working` 일 때다 — 런이 돌고 있으면 그 대화에, 아니면 다음 단계나 재개 때 실린다. `blocked` · `review` 의 런에는 넣지 않는다 (곧 끊기거나 끝날 대화다). 멘션한 직원 가운데 담당이 아닌 구성원의 읽기 턴을 넣는다 — 담당의 그 방 턴은 작업 런이라 따로 넣지 않는다. owner 의 글은 `exchange_count` 에 세지 않는다 (§4.6) |
| `department` · `group` | 턴을 넣는다 (아래 '단체방의 턴'. 파트방의 구성원은 그 파트의 재직 직원이다): `mentions` 가 있으면 그 직원만, 없으면 빠른 판단(아래 '단체방의 턴' T5)에서 '예' 인 직원이 정해진 순서로 한 명씩 받는다. `exchange_count` 가 0 이 아니면 0 으로 돌리고 `room.updated` 를 낸다 (§4.6). 안 읽음과 마지막 글은 다른 방과 같다. 보관한 단체방(owner 가 나간 방, §3.6)에는 쓸 수 없다 — `precondition_failed`. 구성원이 없는 파트방에 쓴 글은 저장만 하고 턴이 없다. 보관하기 전에 저장된 `client_msg_id` 의 재시도는 그래도 200 과 그 글이다 (중복 방지를 먼저 본다) |
| `system` | owner 는 쓸 수 없다 — `precondition_failed` |

**단체방의 턴.** (`studio/chat.py` 의 `ChatTurns.group`. `Hooks.group_turns` 가 부른다) 파트방(`department`)도 같은 줄을 탄다 — 아래 '단체방'은 파트방으로 읽어도 된다. 다른 점은 프롬프트의 방 호칭이 '파트방'이고 구성원이 그 파트의 직원이라는 것뿐이다.

- 정해진 순서는 재직 중인 지금 구성원의 `(joined_at, employee_id)` 오름차순이다 — `GET /studio/rooms/{id}` 의 `members[]` 만으로 앱이 같은 순서를 낸다. 퇴사한 직원과 내보낸 직원은 턴을 받지 않는다. owner 가 멘션한 직원들은 멘션한 순서로 받는다.
- 한 방의 턴은 한 번에 하나다. 방마다 차례를 기다리는 줄이 있고, 앞 직원의 턴이 끝난 뒤에 다음 직원의 턴을 만든다 — 그래서 뒤 직원의 턴에는 앞 직원이 방금 남긴 글이 들어 있다. 글 쓰기의 응답은 턴을 기다리지 않는다 (1:1 방과 같다).
- 각 턴은 그 직원의 `room_members` 행을 잇는 채팅 턴이다 (§3.7 · §9.5): `fed_message_id` 뒤에 쌓인 글을 글쓴이 이름과 함께 읽고, 그 끝에 방 이름 · 구성원(이름과 id) · 자기가 누구인지 · 넘어가는 법 · 다른 직원을 부르는 법을 적은 `시스템:` 줄이 붙는다. 할 수 있는 일은 1:1 방의 채팅 턴과 같다. 왕복 한도의 두 시스템 글(`group_exchange_limit` · `group_continued`)과 '답할 직원이 없다' 글(`group_nobody`)은 owner 가 읽을 글이라 턴에 넣지 않는다.
- 자기 일이 아닌 직원은 넘어간다: 답(`output`)을 비우고 `room_post` 도 쓰지 않으면 방에 글이 남지 않는다 (§9.5).
- 직원이 `room_post` 로 다른 구성원을 멘션하면 그 직원의 턴이 줄 끝이 아니라 **지금 턴 바로 다음**에 들어간다 (§8.4. 줄에 기다리는 직원이 있어도 그보다 앞선다. 한 턴이 여럿을 멘션하면 멘션한 순서로 이어서 들어간다). 그래서 A→B→A 로 이어 가는 말이 줄 뒤쪽 직원의 턴에 끊기지 않는다. 이미 차례를 기다리는 직원도 한 번만 받되 지금 턴 바로 다음 자리로 옮겨진다 (owner 의 글로 선 턴이면 그 표시가 남아 왕복 수에 세지 않는다). 이렇게 들어온 턴을 방 단위로 세고 한도에서 멈춘다 (§4.6). 왕복 한도 · 멈춤 · 이어 가기와 owner 의 글이 우선하는 규칙은 그대로다.
- **빠른 판단 (T5).** owner 의 글에 멘션이 없으면 방의 직원 전원이 아니라 '예' 라고 한 직원만 턴을 받는다. 그 줄의 첫 턴 차례가 올 때 구성원 전원에게 가벼운 모델(`Config.studio_group_judge_model`, 기본 `haiku`)을 **동시에** 한 번씩 부른다 — 글을 쓰는 요청이 모델을 기다리지 않는다. 재료는 그 직원의 이름 · 파트 · 맡은 일 한 줄(PERSONA.md 의 `- 맡은 일:` 줄), 방의 최근 글 요약(§9.1 의 '방의 최근 글'과 같다), 새 글이고, 답은 `예`/`아니오` 와 짧은 이유다. '예' 인 직원만 방의 순서(`group_order`)대로 줄에 선다. 호출이 실패하거나 20초를 넘기거나 예/아니오로 읽히지 않으면 '예' 로 친다 — 판단이 잘못 막는 쪽보다 쓸데없이 한 번 더 답하는 쪽이 싸다. 아무도 '예' 가 아니면 시스템 글 하나(`meta.event = group_nobody`, '답할 직원이 없다고 판단했습니다 — 멘션하면 그 직원이 답합니다')를 남기고 턴은 없다. 이 글도 owner 가 읽을 글이라 턴에 넣지 않는다. owner 가 멘션한 직원과 직원이 멘션한 직원은 판단 없이 턴을 받는다. 설정이 빈 값이거나 `claude` 가 없으면 판단 없이 전원이 턴을 받는다 (이전과 같다). 판단 호출의 사용량은 usage DB 에 `run_id = room:<방 id>`, `stage = studio:group_judge` 로 적는다. 직원의 턴 안내에는 '다른 구성원의 몫이라 넘길 때도 그 직원을 멘션한다' 가 실린다 — 판단에서 빠진 직원은 멘션으로 불러야 답한다.
- 줄은 서버 메모리에 있다. 서버를 다시 띄우면 남은 차례는 사라지고, owner 의 다음 글이 다시 넣는다. 나중에 초대된 직원의 `fed_message_id` 는 0 이라, 첫 턴에 방의 처음 글부터 50개씩 읽는다 (1:1 방의 턴과 같은 쪽 나눔이다).
- 1:1 방에서 직원이 다른 직원을 멘션해도 턴은 들어가지 않는다 (§8.4).

- `mentions` 는 그 방의 구성원인 직원 id 여야 한다. 아니면 `precondition_failed`.
- `client_msg_id` 는 중복 방지용이다. `meta.client_msg_id` 에 넣고, 같은 방의 최근 글에 같은 값이 있으면 새로 만들지 않고 그 글을 돌려준다 (잠정, S-3). 이때 응답은 201 이 아니라 200 이고 `message.created` 를 다시 내지 않는다. '최근'은 그 방의 마지막 50개 글이다 (S-3).
- 첨부는 기존 `/attachments`(`server.py` 151–165)와 같은 base64 JSON 방식이다. `POST /studio/attachments` 가 돌려준 `id` 를 글의 `attachments[]` 에 넣으면, 서버가 파일을 `runs/studio/attachments/<글 id>/` 로 옮긴다 (§2.2). 그 `id` 는 `att_<hex12>` 이고 한 번만 쓸 수 있다. 글에 붙기 전의 파일은 `runs/studio/attachments/_staged/<id>/` 에 있고 `GET /studio/attachments/...` 로는 받을 수 없다. 없는 `id` 는 `precondition_failed` 다. 한 글에 같은 이름이 둘이면 뒤의 것에 `-2` 를 붙인다 (S-3).
- `messages.attachments_json` 의 모양을 확정한다: `[{name, path, mime, size}]`. `path` 는 `runs/studio/attachments/` 기준 상대 경로 `<글 id>/<name>` 이고(결재에 딸린 파일은 `<결재 id>/<name>`, 아래), 받는 주소는 `GET /studio/attachments/<path>` 다.

**직원의 첨부** (`room_post` 의 `attachments[]` 는 S-6 확정 — `studio/files.py`. 런의 작업 폴더는 `api.install` 의 `workdir` 콜백 = `studio/runs.py` 의 `workdir(engine, run_id)` 로 얻는다. `figure` · `evidence[].ref` 의 복사는 잠정, S-11). 직원은 `POST /studio/attachments` 를 부를 수 없고(owner 경로, §6.2) 파일을 올리는 MCP 도구도 따로 두지 않는다 — 첨부용 도구는 늘리지 않는다(지금은 T5 의 둘을 더해 14개). 파일을 받는 도구 인자(§8.4: `room_post` 의 `attachments[]` · `ask_owner` 의 `options[].figure` · `task_request_review` 의 `evidence[].ref`)에 파일 경로를 넘기면 서버가 검사한 뒤 복사한다. CAMPAIGN.md 는 직원이 파일을 붙이는 방법을 적지 않는다. `[§12]`

- 입력은 `{path, name=""}` 다 (`evidence[].ref` 는 경로 문자열 하나). `path` 는 절대 경로이거나 그 런의 작업 폴더 기준 상대 경로다. `name` 을 비우면 파일 이름을 쓴다.
- 검사는 서버가 한다. MCP 서버는 파일을 읽지 않고 경로만 넘긴다 — 검사가 런 쪽 프로세스에 놓이면 폴더 밖 파일을 막을 수 없다. 서버는 토큰의 `run` 으로 그 런의 작업 폴더(단계가 도는 폴더. 워크스페이스가 있으면 그 경로, 없으면 `RunState.workdir`, `pipeline.py` 1551)를 찾아 `path` 를 `resolve()` 하고, 결과가 허용 폴더 안의 파일인지 본다. `_attachment_paths`(`server.py` 168–176)의 `root not in p.parents` 와 같은 방식이다.
- 허용 폴더는 둘이다: 그 런의 작업 폴더, 그리고 프로젝트 폴더(`projects.path` — 지금 작업의 프로젝트, 작업이 없으면 호출자가 배치된 프로젝트). `copy` 런은 작업 폴더가 복사본이라, 에디터가 원본 프로젝트에 남긴 파일을 붙이려면 둘째가 필요하다. 작업도 배치된 프로젝트도 없으면(인사 · 미배치 직원, `employees.project_id` 가 NULL — §3.5) 둘째 폴더는 없고 그 런의 작업 폴더 하나만 허용한다. `task_propose` 가 인사에게 `project_id` 를 요구하는 것(§8.4)과 같은 결이다. 작업 없는 채팅 턴 런의 작업 폴더는 §9.5 의 덧붙임이 정한다 — 직원이 배치된 프로젝트의 폴더이고, 미배치 직원과 인사는 그 직원 폴더다 (S-7 확정 — `studio/chat.py` 의 `_folder`. 프로젝트의 폴더는 그 프로젝트 `repo` 의 폴더다, §9.5).
- 한도(파일당 25MB)와 이름 정리(`server.py` 161)는 owner 의 첨부와 같다. 서버가 파일을 `runs/studio/attachments/<글 id 또는 결재 id>/<name>` 으로 복사하고 — 원본은 그대로 둔다 — owner 의 첨부와 같은 `{name, path, mime, size}` 로 적는다.
- 결재에 딸린 파일(`figure` · `evidence[].ref`)은 `attachments/<결재 id>/` 에 둔다. 결재에는 글 id 가 없기 때문이다. 글 id 는 정수이고(§3.8) 결재 id 는 `inb_<hex8>` 이라(§3.1 · §3.13) 폴더 이름이 겹치지 않는다. 받는 주소는 글의 첨부와 같다.
- 실패는 `precondition_failed` 이고 `failed[]` 에 걸린 것을 적는다(모양은 §6.3): `attachment_not_found`(파일이 없거나 파일이 아니다) · `attachment_outside_workspace`(허용 폴더 밖이다) · `attachment_too_large`(25MB 를 넘는다). 검사는 글 · 결재를 만들기 전에 모두 끝낸다 — 하나라도 실패하면 글 · 결재 · 전이를 만들지 않는다. 한 파일의 검사 순서는 허용 폴더 안인지 → 파일인지 → 크기다 — 폴더 밖 경로는 파일이 있든 없든 `attachment_outside_workspace` 라 다른 폴더의 사정을 알리지 않는다. 작업 폴더를 얻지 못한 런의 상대 경로도 같은 코드다 (S-6).
- 에디터 스크린샷의 위치 (M-7 이 정했다): 서버가 정해 에디터에 넘긴다 — `runs/studio/attachments/shot_<hex12>/viewport-<YYYYMMDD-HHMMSS>.png`. 프로젝트 폴더에 쓰지 않아 git 상태가 깨끗하고, 직원 첨부의 허용 폴더는 넓히지 않는다 — 올리는 것은 서버다(`POST /studio/projects/{id}/editor/screenshot`, §6.4). `room_id` 가 없으면 그 폴더가 받는 주소이고(`attachment.path` = `shot_<hex12>/<name>`, `message` 는 `null`), 있으면 글을 만든 트랜잭션 안에서 `attachments/<글 id>/` 로 복사한 뒤 `shot_` 폴더를 지운다. 방에 올리는 글은 `author_kind = system`, `body` 는 "<프로젝트 이름> 에디터 뷰포트", `meta = {event: "editor.screenshot", project_id}` 이고 `message.created` 가 나간다 — owner 의 글이 아니라 직원 턴을 일으키지 않는다. 응답의 `attachment` 는 다른 첨부와 같은 `{name, path, mime, size}` 다. 실패하면 `shot_` 폴더를 남기지 않는다. `room_id` 없이 찍은 `shot_` 폴더는 임시 보관이다 — 24시간이 지난 것은 다음 스크린샷을 찍을 때 지우고(`editor.prune_shots`. 이름이 `shot_<hex12>` 인 폴더만 본다 — 글 · 결재의 폴더는 건드리지 않는다), 그 뒤 그 `attachment.path` 는 404 다. 오래 둘 그림은 `room_id` 로 방에 올리거나 다른 곳으로 복사한다 — 검수 패키지의 `screenshots[]` 도 `shot_` 주소를 그대로 적지 않는다 (M-7 이 정했다. 24시간은 재 보지 않은 값이다).

## 7. WS

근거: S-4, S-7, M-8, §4.2 의 WS 열. §1.5 의 잠정 이름 8개를 그대로 확정하고 8개를 더한다.

### 7.1 연결과 인증

- 주소는 `WS /studio/ws?after_id=<n>` 이다.
- 인증은 핸들러가 직접 한다. 기존 가드는 `@app.middleware("http")`(`server.py` 90)라 WebSocket 연결을 거치지 않는다 (S-4 가 테스트로 확인했다 — `tests/test_studio_ws.py`). 순서는 §6.2 와 같다: 출처 · 네트워크 검사 → 토큰. 출처 검사는 `Origin` 헤더가 있으면 `Host` 와 같아야 통과시킨다 — 기존 가드는 GET 의 출처를 보지 않는데 WS 핸드셰이크가 GET 이라, 그대로 따르면 검사가 없는 것과 같다. 네트워크 검사는 가드와 같은 함수(`server.py` 의 `_network_allowed`)다. 거부할 때도 연결을 받아들인 뒤에 종료 코드로 끊는다 — 받아들이기 전에 끊으면 코드가 클라이언트에 닿지 않는다 (S-4).
- 토큰은 `Authorization: Bearer` 헤더로 받는다(앱). 헤더를 붙일 수 없는 브라우저는 연결 뒤 첫 메시지로 `{type: "auth", token}` 을 보낸다. 주소의 쿼리에는 싣지 않는다 (로그에 남는다). 루프백도 토큰을 요구한다 — 헤더도 `auth` 메시지도 없으면 4401 이다 (§6.2 의 4). 대시보드는 studio 화면에서 받은 공용 토큰을 `auth` 로 보낸다 (§6.2).
- owner 만 받는다. 행위자 토큰은 거부한다.
- 거부할 때의 종료 코드: 토큰이 없거나 틀리면 4401, 출처 · 네트워크 · 주체가 맞지 않으면 4403. 연결 뒤 10초 안에 인증이 끝나지 않으면 4401 (잠정, S-4).
- 기기를 폐기하면 그 기기의 연결을 4401 로 끊는다 (§6.5). 폐기 라우트(M-2)가 폐기를 커밋한 뒤 `ws.hub.drop_device(device_id)` 를 부른다 — 부르지 않으면 이미 열린 연결은 남는다 (S-4). 라우트는 `ws` 를 import 하지 않는다: `ws.install` 이 `api.on_revoke(hub.drop_device)` 로 등록해 둔 것을 부른다 (`api.on_commit` 과 같은 방식, M-2). `drop_device` 는 부른 순간 등록돼 있는 연결만 본다. 그래서 토큰 확인과 연결 등록 사이에 폐기가 끝난 기기 연결은, 서버가 연결을 등록한 직후 그 기기의 `revoked_at` 을 다시 읽어 `hello` 없이 4401 로 끊는다 (읽지 못하면 1013). 공용 토큰 연결은 읽지 않는다 (M-2).

연결 뒤의 순서:

1. 서버가 `{type: "hello", last_id, now, machine}` 을 보낸다. `last_id` 는 그 순간 `stream_events` 의 가장 큰 id 다.
2. `after_id` 가 있으면 `after_id < id` 인 저장된 이벤트를 id 순으로 보낸다. 없으면 건너뛴다 (지금부터 받는다).
3. 그 뒤로 새 이벤트를 id 순으로 보낸다.

- `after_id` 가 보관 범위(§7.2)보다 오래됐으면 `{type: "resync", oldest_id}` 를 보내고 3부터 한다. 앱은 REST 로 전부 다시 읽는다.
- `after_id` 가 `last_id` 보다 크거나 정수가 아니어도 `resync` 다 — 이 DB 의 순번이 아니다 (DB 를 새로 만들었거나 다른 컴퓨터의 커서다). `oldest_id` 는 남아 있는 가장 작은 id 이고, 남은 행이 없으면 `last_id + 1` 이다. `last_id` 는 보관 정리로 행이 지워져도 줄지 않는다. `hello` 의 `machine` 은 `{name, server_version, schema_version, utc_offset_minutes}` 다 — `GET /studio/machine` 의 같은 이름 값이고, 연결마다 나가므로 읽는 데 시간이 드는 칸(`doctor` · `editors` 등)은 싣지 않는다 (M-1 확정).
- 서버는 30초마다 `{type: "ping"}` 을 보낸다. 클라이언트가 보내는 메시지는 `auth` 와 `pong` 뿐이다. 그 밖의 것은 무시한다.
- `hello` · `ping` · `resync` 는 제어 메시지라 `id` 가 없다.

### 7.2 stream_events — 이어 받기의 저장소

13번째 테이블이다 (§3.16). `[§12]` owner 확정 (§12.6 의 7): 테이블을 하나 더한다.

```sql
CREATE TABLE stream_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,       -- after_id 가 가리키는 순번
    type        TEXT NOT NULL,                           -- §7.4
    at          TEXT NOT NULL,
    project_id  TEXT,                                    -- 아래 셋은 걸러 읽기용. FK 없음
    room_id     TEXT,
    task_id     TEXT,
    data_json   TEXT NOT NULL DEFAULT '{}'
);
```

- 마이그레이션 버전 2 다. §3.2 가 버전 1 을 12개 테이블로 고정했기 때문이다. S-2 의 전이가 처음부터 이벤트를 쓰도록 S-1 이 버전 1 과 함께 내보낸다.
- 쓰는 때: 상태를 바꾸는 트랜잭션 안에서 같이 쓴다 (§4.5 실행 순서 1). 저장소가 `transaction()` 안에서 부르는 `emit(tx, type, data, *, project_id, room_id, task_id)` 를 둔다. 쓴 행의 id 를 돌려주고 `tx.emitted` 에 모은다. §4.5 의 2 "커밋 뒤 WS 이벤트를 낸다"는 커밋 뒤에 연결들에 밀어 주는 걸음이다.
- 이렇게 하는 이유: S-4 의 완료 기준이 "끊었다 붙어도 빠진 이벤트 없이 순서대로"다. 메모리 버퍼는 서버 재시작에서 끊기고, 상태는 바뀌었는데 이벤트가 없는 틈이 생긴다. 같은 트랜잭션이면 둘이 함께 남거나 함께 없다.
- 순서: 쓰기가 `BEGIN IMMEDIATE` 와 잠금 하나로 줄 서므로(§3.1) id 순서가 커밋 순서다.
- 보내는 방법: 연결마다 마지막으로 보낸 id 를 기억하고, 커밋 알림이 오면 그보다 큰 행을 DB 에서 읽어 보낸다. 메모리 큐가 아니라 DB 를 기준으로 삼으므로 2와 3 사이(§7.1)에 빠지거나 겹치지 않는다.
- 보관: 7일 또는 5만 행 중 먼저 닿는 쪽까지 두고, 서버 시작 때와 하루 한 번 오래된 행을 지운다 (잠정, S-4). §3.1 의 "행을 지우는 테이블은 `task_deps` 뿐"에 예외가 하나 생긴다. `[§12]`
- 저장하지 않는 이벤트는 `run.live` 와 `job.log` 다. 자주 나오고, 놓쳐도 다음 것이 덮는다.

### 7.3 봉투

```json
{"id": 1234, "type": "task.transitioned", "at": "2026-10-10T13:10:47",
 "project_id": "prj_1a2b3c4d", "room_id": null, "task_id": "task_5e6f7a8b",
 "data": {}}
```

- `id` 는 `stream_events.id` 다. 저장하지 않는 이벤트는 `id` 가 `null` 이고, 앱은 `after_id` 를 올리지 않는다.
- `data` 안의 행은 REST 와 같은 모양이다 (§6.1). 앱은 이벤트만으로 화면을 고칠 수 있다.
- 걸러 보내지 않는다. 한 컴퓨터의 owner 는 하나라 모든 연결이 모든 이벤트를 받는다 (잠정, S-4).

### 7.4 이벤트

| `type` | 저장 | 내는 때 | `data` |
|---|---|---|---|
| `message.created` | ● | 방에 글이 저장됐다 (owner · 직원 · 시스템) | `{message}` — §3.8 의 행 |
| `room.updated` | ● | 방이 생겼다, 구성원 · 음소거 · `owner_read_id` 가 바뀌었다. 단체방은 이름이 바뀐 때와 보관한 때, `exchange_count` 가 바뀐 때에도 낸다 (§4.6 · §6.4). 단체방의 시스템 글과 함께 나갈 때는 이것이 그 글의 `message.created` 보다 먼저다 | `{room, members[]}` |
| `room.deleted` | ● | 단체방을 지웠다 (`DELETE /studio/rooms/{id}`, §6.4). 앱은 그 방의 행과 글을 버린다. 방이 없어진 뒤라 이 이벤트가 그 방의 마지막이다 | `{room}` — 지우기 전의 방 행 |
| `room.cleared` | ● | 1:1 방의 글을 지웠다 (`POST /studio/rooms/{id}/clear`, §6.4). 방은 남는다. 앱은 그 방의 글 목록을 비우고 읽지 않은 수를 0 으로 한다 | `{room, members[]}` — `room.updated` 와 같은 모양 |
| `task.created` | ● | C1 · C2 | `{task}` |
| `task.transitioned` | ● | T1–T11. 작업 취소(T10 · T11)는 `to` 가 `cancelled` 다 | `{task, rule, from, to, actor, reason, event_id, inbox_id}` |
| `task.updated` | ● | 전이가 아닌 작업 변화 | `{task, change, event_id}` |
| `inbox.created` | ● | 결재가 생겼다 | `{inbox}` — payload 포함, `package` 제외 |
| `inbox.decided` | ● | 결재가 결정됐다 | `{inbox}` |
| `inbox.cancelled` | ● | 결재가 취소됐다 (§5.1). T11 이 닫은 차단 결재도 여기다 | `{inbox}` |
| `employee.status` | ● | §4.9 의 값이 바뀌었다 | `{employee_id, status, prev}` |
| `employee.updated` | ● | 직원이 생겼다(`hire` 승인), 이름 · 배치 · 휴직 · 퇴사 · 파일이 바뀌었다 | `{employee, change}` |
| `campaign.updated` | ● | 캠페인이 생겼다, 상태 · 진행 방식이 바뀌었다 | `{campaign}` |
| `project.updated` | ● | 프로젝트가 등록 · 수정 · 보관됐다 | `{project}` |
| `job.step` | ● | 프로젝트 생성의 단계가 시작 · 끝 · 실패했다 (M-8) | `{job_id, kind, step, index, total, status, error}` |
| `job.log` | | 그 단계의 로그 꼬리 | `{job_id, step, lines[]}` |
| `job.finished` | ● | 생성 작업이 끝났다 | `{job_id, kind, status, project_id, error}` |
| `run.live` | | 도는 studio 런의 liveness | 아래 |

- `task.transitioned` 의 `actor` 는 `{kind, id}`, `event_id` 는 `task_events.id`, `rule` 은 §4.2 의 번호다. `inbox_id` 는 그 전이가 만들거나 결정한 결재다. T11 은 닫은 차단 결재이고 T10 은 `null` 이다.
- `task.updated` 의 `change` 는 `task_events.kind` 중 전이가 아닌 것이다: `queued` · `unqueued` · `assigned` · `edited` · `deps` · `progress` · `run` · `limit` · `flag` · `guard` (§3.12). `run` 은 런 시작 · 재개 · 멈춤이고, 검수 패키지를 다시 읽을 신호다 (§5.4).
- `task_events` 행이 없는 `task.updated` 가 하나 있다 (S-9): `change = exchange` — 직원의 멘션이 턴을 넣어 `exchange_count` 가 올랐다 (§4.6). 이때 `event_id` 는 `null` 이다. 세는 것은 열이고 기록은 그 방의 글이라 이벤트 행을 따로 두지 않는다.
- `employee.updated` 의 `change` 는 `created` · `renamed` · `assigned` · `leave` · `retired` · `file` 이다. `file` 은 H-3 확정이다 — 직원 폴더의 파일이 바뀌었다는 뜻이고 `data` 는 `{employee, change}` 뿐이다(어느 파일인지는 싣지 않는다. 앱은 `GET /studio/employees/{id}` 를 다시 읽는다). 직접 수정 · 되돌리기 · `guide_amend` 승인이 낸다 (§9.6). `renamed` · `assigned` · `leave` · `retired` 는 H-3 확정이다 — 각각 `PATCH` · `assign` · `leave` · `retire` 가 낸다 (§6.4). `data` 는 `{employee, change}` 이고 `employee` 는 바뀐 뒤의 행이며 이벤트의 `project_id` 도 바뀐 뒤 행의 값이다. 휴직은 `employee.status` 다음에 `employee.updated` 를 쓴다. 값이 그대로면 내지 않는다.
- 한 트랜잭션이 여러 이벤트를 쓸 때는 §4.2 표의 WS 열에 적힌 순서로 쓴다. 예: T4 는 `inbox.created` → `task.transitioned` → `employee.status`. 결정이 남기는 시스템 글(`message.created`)은 그 뒤다.

**프로젝트 생성 단계 (`job.step`).** `kind` 는 `project_create` 다. `step` 은 M-8 의 순서대로 `folder` → `build` → `plugins` → `harness` → `register` 이고, `status` 는 `running` · `done` · `failed` · `skipped` 다. `job.finished` 의 `status` 는 `done` · `failed` · `cleaned`(만든 것을 정리했다)다. 실패한 단계부터의 재시도와 정리는 §6.4 의 `/studio/jobs/{id}/retry` · `/cleanup` 이다 (M-8 확정. `studio/jobs.py`).

- `index` 는 1부터, `total` 은 5다. `error` 는 실패한 단계에서만 글이고 그 밖에는 `null` 이다. 단계마다 `running` 을 먼저 내고 `done` · `failed` · `skipped` 중 하나로 끝낸다 — 건너뛴 단계도 `running` 다음에 `skipped` 다. 실패한 단계 뒤의 단계는 이벤트가 없고 job 파일에 `pending` 으로 남는다.
- `job.finished` 는 마지막 `job.step`(다섯째 단계의 끝이거나 `failed`)과 같은 트랜잭션에서 그 뒤에 쓴다. `error` 는 `<단계>: <그 단계의 error>` 이고 성공이면 `null` 이다. 봉투의 `project_id` 는 `register` 가 행을 넣은 뒤(그 단계의 끝과 `job.finished`)에만 채워진다. `register` 가 내는 `project.updated` 는 그 단계의 `running` 과 `done` 사이에 온다. 단계 밖에서 중단된 job(예: job 파일을 쓰지 못했다)은 돌던 단계를 `failed` 로 적고 그 `job.step` 과 `job.finished` 를 같은 트랜잭션에서 이 순서로 낸다. 이때 두 `error` 는 단계 이름 없이 같은 글 "생성 작업이 중단됐다 (<예외 이름>): <예외 글>" 이다. 돌던 단계가 없으면 `job.finished` 만 낸다.
- 재시도한 job 은 건너뛴 단계의 `job.step` 을 다시 내지 않는다. 다시 도는 첫 단계의 `running` 부터 이어지고 끝에 `job.finished` 를 다시 낸다 — 한 job 에 `job.finished` 가 여러 번 올 수 있다. 정리가 끝나면 `job.step` 없이 `job.finished` 하나만 낸다: `status` 는 `cleaned`, `error` 는 `null`, `project_id` 는 지운 프로젝트 행의 id 다 (행이 없었으면 `null`). 다 지우지 못한 정리는 이벤트를 내지 않는다. 서버가 시작할 때 실패로 바꾼 job 은 중단된 job 과 같은 순서로 내고, 글은 "생성 작업이 중단됐다 (서버가 다시 시작됐다)" 다.
- `job.log` 의 `lines[]` 는 그 단계 로그의 마지막 40줄 전체다 — 덧붙일 조각이 아니라, 받은 것으로 바꿔 그린다. 단계의 첫 줄에서 한 번, 그 뒤로는 1초에 한 번 이하(`jobs.LOG_EVERY`), 단계가 끝날 때 못 보낸 것이 있으면 한 번 더 보낸다. 저장하지 않으므로(§7.2) 저장 이벤트와의 앞뒤는 보장하지 않는다 — 끝난 단계의 마지막 `job.log` 가 그 단계의 `job.step` 뒤에 올 수 있다. 보내는 자리는 `ws.hub.say(type, data)` 다 (`api.on_live`). 다시 붙은 화면은 `GET /studio/jobs/{id}` 의 `steps[].tail` 을 읽는다. `hub.say` 로 보내는 것은 연결마다 `run.live` 와 다른 대기열에서 기다린다. 각각 최대 100개이고 넘치면 오래된 것부터 버린다 — `job.log` 가 `run.live` 를 밀어내지 않는다. 한 라운드에서 저장 이벤트, `run.live`, `job.log` 순서로 보낸다.

**런 liveness (`run.live`).** `data` 는 `{run_id, employee_id, room_id, task_id, mode}` 에 `engine.liveness(run_id)`(`pipeline.py` 743–778)가 돌려주는 필드를 그대로 더한 것이다.

| 필드 | 뜻 |
|---|---|
| `status` | 런 상태 (§1.5) |
| `alive` | 런이 `running` 이고 프로세스가 살아 있다 |
| `stage` · `stage_seconds` | 지금 단계와 그 단계가 시작한 뒤의 초 |
| `quiet_seconds` | 마지막 출력 뒤의 초 |
| `doing` · `turn` · `snippet` | pulse 가 적은 값 그대로. `snippet` 은 마지막 출력의 끝 160자다 (772) |
| `tool` · `target` · `tools` · `tool_seconds` | pulse 가 적은 도구와 대상, 그 도구를 쓴 초 (773–775). `doing` 은 문자열(`start` 로 시작하고 `retry` 등으로 바뀐다), `turn` 은 지금까지의 턴 수(정수), `tools` 는 지금까지 쓴 도구 수(정수)다 — `runners.py` 의 `LiveDetail.pulse`(194–196)와 691 · 713 (S-4 가 읽었다). pulse 가 없으면 셋 다 `null` 이다 |
| `last` | 마지막 런 이벤트 `{kind, stage, detail}` |
| `precise` | `false` 면 pulse 없이 활동 기록만으로 판단했다 |

- `mode` 는 `work`(작업 런) · `read`(작업방의 읽기 전용 턴) · `chat`(1:1 방 · 파트방 · 단체방의 채팅 턴)이다. 작업 런은 `working` 인 작업의 `tasks.run_id`(S-8 이 채운다)로 잡힌다. 채팅 · 읽기 턴은 작업의 런이 아니므로 S-7 · S-9 가 턴을 시작할 때 `api.track_run`, 끝낼 때 `api.untrack_run` 을 부른다 — 부르지 않으면 그 턴의 `run.live` 는 나가지 않는다 (S-4).
- 보내는 때: 런이 시작 · 재개될 때, 도는 동안 2초마다(값이 바뀌었을 때만), 런이 멈출 때 한 번 더(`alive = false`). 커밋이 알린 변화는 2초를 기다리지 않고 바로 읽되 두 읽기 사이는 0.5초 이상 띄운다 — 커밋이 몰려도 읽기와 전송이 그보다 잦아지지 않고, 시작 · 멈춤 보고는 최대 0.5초 늦는다. 연결이 하나도 없으면 읽지 않는다 (간격 잠정, S-4).
- 저장하지 않으므로 다시 붙은 앱은 `GET /studio/runs/{run_id}/live`(§6.4)로 한 번 읽는다. "지금 답하는 중"(§4.9)은 `mode = chat` 인 `run.live` 로 본다.
- 단체방의 턴도 `mode = chat` 이고 `employee_id` · `room_id` 가 그 턴의 직원과 방이다 (`task_id` 는 `null`). 한 방의 턴은 한 번에 하나라 (§6.6), 단체방에서 지금 답하는 직원은 `room_id` 가 그 방인 `run.live` 의 `employee_id` 한 명이다. 차례를 기다리는 직원은 `run.live` 에 나오지 않는다.

### 7.5 §4 와의 대조

§4.2 표의 WS 열과 §4 본문이 가리키는 이벤트는 모두 §7.4 에 있다.

| §4 의 자리 | 이벤트 |
|---|---|
| C1 · C2 | `task.created` (+ C1 은 `inbox.created`, C2 · T1 은 작업방의 `room.updated`) |
| T1 · T2 · T5 · T7 · T8 · T9 | `inbox.decided` · `task.transitioned` (+ `employee.status`) |
| T10 · T11 (작업 취소) | `task.transitioned` (+ T11 은 그 앞에 `inbox.cancelled`, 뒤에 `employee.status`) |
| T3 의 시작 요청 · 줄에서 빠짐 | `task.updated` (`queued` · `unqueued`) |
| T3 · T4 · T6 | `task.transitioned` · `employee.status` · `inbox.created` · `run.live` |
| §4.5 의 `guard` · `limit` · `run` 행 | `task.updated` |
| T8 · T9 의 선행 반려 표시 | `task.updated` (`flag`) + 시스템 글 (§5.9) |
| §4.9 | `employee.status` |

### 7.6 글의 `meta`

`messages.meta_json`(§3.8)의 모양을 확정한다. 모든 키는 선택이다.

| 키 | 뜻 |
|---|---|
| `event` | 시스템 글의 종류: `inbox.created` · `inbox.decided` · `needs_assignment` · `upstream_rejected` · `run_error` · `invariant_broken` · `daily_report` · `greeting` · `handoff` · `group_invited` · `group_removed` · `group_renamed` · `group_exchange_limit` · `group_continued` · `group_nobody` · `staffing.signal` · `disk_low` · `disk_ok` · `usage_high` |
| `task_id` · `inbox_id` · `run_id` | 카드가 가리키는 것 |
| `date` · `from` · `to` | `daily_report` 글이 다룬 날(서버 현지 날짜 `YYYY-MM-DD`)과 구간(`from <= 시각 < to`, 서버 현지 시각). `date` 가 같은 날 다시 쓰지 않는 근거다 (§5.9, S-13) |
| `free_bytes` · `min_free_gb` · `path` | 시스템 글 `disk_low` · `disk_ok` 를 쓸 때 읽은 여유(바이트)와 그때의 임계(GB), 물어본 드라이브의 경로. 가장 새 디스크 글의 `event` 가 다시 알릴지 가리는 근거다 (§5.9, V-3) |
| `provider` · `window` · `utilization` · `resets_at` · `warn_at` · `held` | 시스템 글 `usage_high` 를 쓸 때의 사용 창과 그 사용률, 초기화 시각, 그때의 알림 임계, 보류 옵션이 켜져 있었는가. (provider, window, `resets_at`) 이 같은 글이 이미 있으면 다시 쓰지 않는다 (§5.9, V-4) |
| `upstream_task_id` | `upstream_rejected` 의 선행 작업 |
| `assigned_event_id` | `needs_assignment` 글을 쓸 때 그 작업의 가장 새 `assigned` 이벤트 id (없으면 0). 다시 알릴지 가리는 데 쓴다 (§5.9) |
| `employee_ids` | 단체방의 시스템 글 `group_invited` · `group_removed` (`event` 의 값이다) 가 가리키는 직원 id 목록. 방을 만들 때의 글은 처음 구성원 전부, 그 뒤는 한 명이다 (§6.4) |
| `title` · `old_title` | 단체방의 시스템 글 `group_renamed` (`event` 의 값이다) 의 새 이름과 그 전 이름 |
| `limit` | 단체방의 시스템 글 `group_exchange_limit` (`event` 의 값이다) 이 닿은 한도 값 (§4.6). `group_continued` 글은 `event` 뿐이다 |
| `key` · `trigger` | 인사 1:1 방의 시스템 글 `staffing.signal` (`event` 의 값이다. 푸시하지 않는다) 이 가리키는 채용 계기의 키(`<파트>:<프로젝트>`, §5.3)와 계기 이름 (§8.4) |
| `client_room_id` | 단체방을 만들 때의 `group_invited` 글에만 있다. 그 방을 만든 요청의 중복 방지 키다 (요청이 준 때만, §6.4) |
| `client_msg_id` | 중복 방지 (§6.6) |
| `handoff` | 파트 간 인계: `{format, 표, source?}` — `format` 은 `spec_table` · `data_items` (§8.4. H-5 확정) |

## 8. studio MCP

근거: S-6, S-9, S-11, S-12, H-2 · H-5 · H-6, 전제 2 · 3. 기존 MCP 서버와의 접점은 §11.3.

### 8.1 형태

- `mcp_servers/studio.py` 는 `katae.py` 와 같은 형태다: `FastMCP("studio")`, `@mcp.tool()` 함수, 서버에 HTTP 로 말하는 `_call`(`katae.py` 29 · 32–41 · 44).
- DB 를 열지 않는다 (§3.1 확정). 도구 하나가 `POST /studio/mcp/<도구 이름>` 하나를 부른다. 요청 본문은 도구 인자, 응답 본문은 §8.3 의 결과다. 검사와 전이는 모두 서버가 한다.
- 받는 환경 변수: `STUDIO_URL`(없으면 `Config.server_url`, `katae.py` 27 과 같다) · `STUDIO_ACTOR_TOKEN`(§8.2).
- `katae.py` 와 다른 점: `_call` 은 오류를 `RuntimeError` 로 올리지만(36–40) studio 는 업무 오류를 결과로 돌려준다 (§8.3).
- 모델에게 보이는 도구 이름은 `mcp__studio__<도구 이름>` 이다. 권한 프로파일에 넣는 방법은 §10.1 이다.

### 8.2 호출자 식별

도구 인자에는 호출자의 직원 id 가 없다. 호출자는 런에 주입된 행위자 토큰으로만 정해진다 — 모델이 다른 id 를 적어 넣을 자리가 없다.

**토큰.** `sat1.<claims>.<서명>`.

| 부분 | 내용 |
|---|---|
| `claims` | JSON `{v: 1, run, emp, room, task, mode, iat}` 를 base64url 로 적은 것. `task` 는 없으면 `null`, `mode` 는 `work` · `read` · `chat` (§7.4) |
| 서명 | `HMAC-SHA256(서버 비밀, claims)` 의 hex |
| 서버 비밀 | 서버가 시작할 때 `secrets.token_bytes(32)` 로 만든다. 메모리에만 둔다 |

**주입.** 엔진이 단계를 시작할 때 MCP 서버의 `env` 를 조립하는 `_stage_mcp`(`pipeline.py` 1406–1414)를 넓힌다. 지금은 `digest` 서버에만 값을 넣는다. 엔진은 직원을 모르므로, studio 가 서버 시작 때 "런 id 와 단계를 주면 `studio` 서버의 `env` 를 돌려주는" 콜백을 엔진에 등록하고 `_stage_mcp` 옆의 `_env_mcp` 가 그것을 부른다 (S-6 확정: `RelayEngine.mcp_env` — 서버 이름 → `(run_id, stage)` 를 받아 `env` 나 `None` 을 주는 콜백의 dict. studio 는 `server.py` 에서 `engine.mcp_env["studio"] = api.mcp_env` 로 등록하고, `env` 는 `STUDIO_URL` · `STUDIO_ACTOR_TOKEN` 이다. `None` 이면 단계의 `mcp` 에 적혀 있어도 붙이지 않고, `env` 를 주면 단계의 도구 유무와 무관하게 붙인다. 콜백이 예외를 내면 이벤트 `mcp_env_failed` 를 남기고 붙이지 않는다. 등록된 콜백이 없으면 앞의 동작 그대로다). 콜백은 런이 작업 런이면 `tasks.run_id` 로, 채팅 · 읽기 턴이면 `chat.py` 가 가진 런 장부로 직원 · 방 · 작업을 찾아 토큰을 만든다. 장부에 없는 런(대시보드 · CLI 런)에는 `studio` 서버를 붙이지 않는다.

- 토큰은 단계마다 새로 만든다. 서버가 재시작하면 비밀이 바뀌어 앞의 토큰은 모두 무효다.
- 러너는 MCP 설정을 임시 폴더의 `mcp.json` 에 쓴다 (`runners.py` 664–674). 그래서 단계가 도는 동안 토큰이 그 파일에 있다. 다른 런의 설정 파일을 읽는 것은 §10.3 의 `secret_access` 가 막는다.

**검사** (행위자 경로 `/studio/mcp/*` · `/studio/guard/*` 의 모든 요청). 헤더가 없거나 토큰이 `sat1.` 모양이 아니면 검사 1 에 걸린 것으로 본다 — 401 `actor_invalid`. 루프백이어도 같다. 예외는 주체가 owner 로 정해지는 한 경우다: 맞는 owner 토큰(공용 · 기기)은 403 `actor_not_allowed` 다 (§6.2).

1. 모양과 서명 (`hmac.compare_digest`). 틀리면 401 `actor_invalid`.
2. `run` 의 런이 있고 `running` 이다. 아니면 401 `actor_invalid` — 끝났거나 끊긴 런의 토큰이다.
3. `emp` 의 직원이 있고 퇴사하지 않았다. 아니면 401 `actor_invalid`.
4. 도구별 권한 (§8.5). 토큰의 `mode` 가 아니라 지금의 DB 로 본다 — 담당인지는 `tasks.assignee_id`, 구성원인지는 `room_members`(`left_at` 이 NULL). `/studio/mcp/*` 만 해당하고, 걸리면 HTTP 200 과 `{ok: false, code: actor_not_allowed}` 다 (§8.3). `/studio/guard/*` 는 1–3 만 본다 (§6.4).

**사칭 거부.** S-6 의 완료 기준 "다른 직원 ID 로 호출 시 거부"는 셋으로 확인한다.

| 시도 | 결과 |
|---|---|
| `claims` 의 `emp` 를 다른 직원으로 바꾼 토큰 | 서명이 맞지 않는다 → 401 `actor_invalid` |
| 직원 A 의 토큰으로 직원 B 가 담당인 작업에 `task_progress` · `task_request_review` | `actor_not_allowed` |
| 구성원이 아닌 방에 `room_post`, 구성원이 아닌 작업방의 작업에 `ask_owner` | `actor_not_allowed` |

남는 구멍은 §6.2 와 같다: 헤더 없는 루프백 요청은 `/studio/*` 에서 401 이므로, 직원이 owner 경로를 부르려면 공용 토큰을 읽어 내야 한다. 남는 것은 그 토큰을 읽어 내는 길과, 루프백을 그대로 믿는 `/studio` 밖의 기존 경로다. 셸에서 보내는 요청은 가드가 막고(§10.3 의 `server_control`), 스크립트 안의 요청은 남는다 (§10.8 의 3).

### 8.3 반환 규약

모든 도구는 dict 하나를 돌려준다.

| 경우 | 모양 |
|---|---|
| 성공 | `{ok: true, end_turn, message, …도구별 필드}` |
| 실패 | `{ok: false, code, message, end_turn, …코드별 덧붙는 필드}` |

- 업무 오류는 예외가 아니라 결과다. 서버는 `/studio/mcp/*` 에 HTTP 200 과 `{ok: false, …}` 로 답한다. 모델이 `code` 와 "턴을 끝내라"를 함께 읽어야 하기 때문이다. HTTP 오류로 답하는 것은 인증 단계와 요청 검증뿐이다 — 401 `actor_invalid`(§8.2 의 검사 1–3), 403 `actor_not_allowed`(맞는 owner 토큰이 올 때만, §6.2), 503 `unavailable`(§6.3), 그리고 요청 검증 오류인 422(§6.3 의 기본 형식. 타입이 틀리거나 도구에 없는 인자다). MCP 서버는 401 을 `actor_invalid` 로, 422 를 `invalid_arguments` 로, 그 밖의 HTTP 오류(401 · 422 가 아닌 것)와 연결 실패를 `server_unreachable` 로 바꿔 아래 모양으로 돌려준다. `invalid_arguments` 는 `end_turn: false` 이고 `message` 에 어긋난 인자를 적는다 — 직원이 인자를 고쳐 다시 부른다 (S-6 확정).
- `message` 는 직원이 읽는 한국어 문장이다. 실패면 무엇이 빠졌고 다음에 무엇을 하면 되는지를 적는다.
- **"이제 턴을 끝내라"** 는 `end_turn: true` 다. 그때 `message` 는 이 문장으로 끝난다: "이제 턴을 끝내라. 도구를 더 부르지 말고, 지금까지 한 일과 기다리는 것을 한 문단으로 적고 멈춰라." 같은 약속을 도구 설명(docstring)에도 적는다.
- 실패에 덧붙는 필드는 §6.3 과 이름 · 모양이 같다: `precondition_failed` 는 `failed[]`, `missing_fields` 는 `fields[]`, `illegal_transition` 은 `from` · `to` 다. `exchange_limit` 은 `inbox_id` 다 (S-9) — 그 호출이 T4 를 해 결재를 만들었으면 그 id, 만들지 않았으면(작업이 이미 결재를 기다리거나 `working` 이 아니다) `null`. 아래 오류 코드 표의 그 밖의 코드에는 덧붙는 필드가 없다. MCP 서버는 서버가 준 필드를 빼지 않고 그대로 넘긴다. MCP 서버가 스스로 만드는 `actor_invalid` · `invalid_arguments` · `server_unreachable` 에는 덧붙는 필드가 없다. REST 의 `detail` 자리는 여기서 `message` 다.

`end_turn` 이 `true` 인 경우:

| 경우 | `ok` | `code` |
|---|---|---|
| `ask_owner` 가 결재를 만들었다 (T4) | `true` | — |
| `task_request_review` 가 결재를 만들었다 (T6) | `true` | — |
| 이미 결재를 기다리는 작업에 `ask_owner` · `task_request_review` (§4.5) | `false` | `illegal_transition` |
| 왕복 한도에 닿은 `room_post` (§4.5, §4.6) | `false` | `exchange_limit` |
| 토큰이 무효다 | `false` | `actor_invalid` |
| 서버에 닿지 못했다 | `false` | `server_unreachable` |

- 서버의 뒤처리: T4 를 만든 응답을 보낸 뒤 대기 시간이 지나도 그 런이 `running` 이면 `RelayEngine.cancel` 로 끊는다 (§4.3 T4). 대기 시간은 `Config.studio_cut_after`(초, 기본 60)이고 타이머를 걸 때마다 읽는다. 0 이하면 바로 끊는다.
  - 진입점은 하나다: `api.answered(rule, task_id, caller_run_id, inbox_id)` → `studio/cutoff.py` 의 `Cutoff.after`. `ask_owner` · `task_request_review` 의 핸들러가 결재를 만든 응답을 내보내기 직전에 부르고, 가드의 T4(S-10)도 이것을 부른다. 예외를 올리지 않는다 — 뒤처리가 실패해도 도구 응답은 그대로 나간다.
  - 타이머를 거는 전이 목록은 상수 `cutoff.TIMED = ("T4",)` 한 곳이다. T6 뒤에는 끊지 않는다 — 같은 진입점을 부르지만 목록에 없어 아무것도 하지 않고, 런이 남은 단계를 마저 돈다 (§4.3 T6).
  - 부른 턴의 런이 작업 런(`tasks.run_id`)이면 타이머를 건다. 다른 런이면(담당이 아닌 구성원의 읽기 턴) 기다리지 않고 바로 끊는다 (§5.7). 건 때 그 런이 이 엔진에서 `running` 이 아니면 타이머도 걸지 않는다 — 끊을 것이 없다.
  - 끊기 전에 다시 본다 (타이머 발화 · 바로 끊기 · 재시작 복구가 같은 판정 `cutoff.cut` 을 쓴다): 작업이 아직 `blocked` 이고, `tasks.run_id` 가 건 때의 런이고, 그 결재가 아직 `pending` 이고, 런이 `running` 일 때만 `cancel` 한다. 그 전에 런이 스스로 멈췄거나 owner 가 먼저 결정했으면(T5) 아무것도 하지 않는다. 같은 작업에 T4 가 다시 오면 앞 타이머는 버린다.
  - 끊어도 `task_events` 행은 남기지 않는다 (서버 로그만). 런은 `cancelled` 로 끝나고 작업은 `blocked` 그대로다 — §4.7 표의 `cancelled` · `blocked` 행이고, T5 뒤에 `advance(resume=True)` 로 끊긴 단계의 대화를 잇는다.
  - 타이머는 메모리에만 있다. 서버가 그 사이 다시 뜨면 재시작 복구가 `blocked` 인데 런이 `running` 인 작업을 끊는다 (§4.8 의 1).
  - 알려진 틈: 다시 본 것과 `cancel` 사이는 잠그지 않는다. 그 사이에 끼어들 수 있는 것이 셋이다.
    - (가) 상태를 확인한 뒤 `cancel` 하기 전에 런이 스스로 멈추면 그 정지가 `cancelled` 로 덮일 수 있다 (owner 의 `stop` 과 같은 틈).
    - (나) 그 사이에 owner 의 결정(T5)이 들어오고 런은 아직 돌고 있으면, 런은 `cancelled` 가 되지만 작업은 이미 `working` 이고 줄 서 있다. §4.7 표의 마지막 행이다 — T4 를 하지 않고, 결정문은 걸음 2에서 들어가고, 슬롯을 놓은 뒤 큐가 같은 런을 `advance(resume=True)` 로 다시 잇는다 (`queue.NOT_TAKEN_UP` 에 `cancelled` 가 없다). 잃는 것은 끊긴 단계의 진행분뿐이다.
    - (다) 그 사이에 T5 가 들어오고, 런이 스스로 멈추고, 큐가 그 런을 이미 다시 이었으면, 줄 밖 `working` 작업의 런을 끊게 된다. §4.7 표의 `cancelled` · `working` 행대로 system 이 T4(`run_cancelled`)를 올린다. 불필요한 질문 하나이고 owner 가 `retry` 로 잇는다. 막으려면 엔진에 조건부 `cancel` 이 필요해 그대로 둔다.

오류 코드 (§4.5 의 다섯에 더한 것 포함):

| `code` | 뜻 | `end_turn` |
|---|---|---|
| `illegal_transition` · `actor_not_allowed` · `precondition_failed` · `missing_fields` · `stale_state` | §4.5 | 위 표의 경우만 `true` |
| `not_found` | 그 작업 · 방 · 초안이 없거나 볼 수 없다 | `false` |
| `no_task_context` | 작업이 없는 턴에서 작업이 필요한 도구를 불렀다 | `false` |
| `exchange_limit` | 직원 간 왕복 한도 (§4.5 가 §8 로 미룬 코드) | `true` |
| `invalid_format` | 인계 형식이 맞지 않는다 (H-5). 글을 만들지 않는다. 이유는 `message` 에, 틀린 칸의 이름은 `fields[]` 에 싣는다 | `false` |
| `invalid_arguments` | 타입이 틀리거나 도구에 없는 인자다 (서버의 422. S-6 확정) | `false` |
| `actor_invalid` | 토큰이 무효다 | `true` |
| `server_unreachable` | 서버가 꺼졌거나 답하지 않는다. 401 · 422 가 아닌 HTTP 오류(403 · 503 등)도 여기에 든다 | `true` |

### 8.4 도구

기본값이 있는 인자는 생략할 수 있다. "지금 작업" · "지금 방"은 토큰의 `task` · `room` 이다.

**모든 직원**

| 도구 | 인자 | 하는 일 | 성공 반환 | 오류 |
|---|---|---|---|---|
| `task_propose` | `title` · `reason` · `spec` · `done_criteria` · `type` · `suggested_assignee_id=""` · `depends_on=[]` · `campaign_id=""` · `project_id=""` | C1. 프로젝트는 지금 작업의 것, 없으면 호출자가 배치된 것 (인사는 `project_id` 필수) | `{task_id, inbox_id}` | `missing_fields` · `precondition_failed` |
| `followup_propose` | `task_propose` 의 인자 + `parent_task_id=""` (기본 지금 작업) | 부모가 연결된 C1 (S-12) | `{task_id, inbox_id, parent_id}` | `missing_fields` · `no_task_context` · `not_found` |
| `task_get` | `task_id=""` (기본 지금 작업) | 읽기 | `{task, depends_on[], dependents[], pending_inbox, recent_events[]}` | `not_found` · `no_task_context` |
| `task_list` | `status=""` · `assignee=""`(`me` 또는 직원 id) · `campaign_id=""` · `limit=50` | 읽기. 호출자가 배치된 프로젝트의 작업 (인사는 전체) | `{items[]}` — 각 `{id, title, status, type, assignee_id, is_milestone}` | |
| `task_progress` | `note` · `percent=None` | 지금 작업에 `progress` 이벤트를 남긴다. `task.updated` | `{event_id}` | `actor_not_allowed` · `no_task_context` |
| `task_request_review` | `summary` · `checklist` · `evidence=[]` · `judgment_calls=[]` (모양은 §5.4. `evidence[].ref` 는 아래) | T6 | `{inbox_id}`, `end_turn: true` | `actor_not_allowed` · `illegal_transition` · `precondition_failed` · `missing_fields` |
| `ask_owner` | `kind`(`question` · `choice` · `approval`) · `title` + kind 별 인자 (아래) | T4 (§5.7) | `{inbox_id}`, `end_turn: true` | `no_task_context` · `actor_not_allowed` · `illegal_transition` · `missing_fields` · `precondition_failed` |
| `room_post` | `text` · `mentions=[]` · `room=""`(기본 지금 방) · `attachments=[]`(각 `{path, name=""}`, 아래) · `handoff=None` | 방에 글을 쓴다. 멘션한 직원의 턴을 넣는다 (S-9) | `{message_id, queued[]}` | `actor_not_allowed` · `precondition_failed` · `exchange_limit` · `invalid_format` |
| `employee_list` | `project_id=""` · `department_id=""` | 읽기. 퇴사한 직원은 뺀다 | `{items[]}` — 각 `{id, name, department_id, project_id, status}` | |
| `staff_request` | `reason` · `role=""` · `task_id=""`(기본 지금 작업) · `department_id=""` · `project_id=""` | 사람이 더 필요하다고 인사에게 알린다. `Staffing.request` 로 가서 기간 규칙(§5.3)을 지난 것만 인사의 판단 턴이 된다. 채용 권장은 인사의 판단이라 보장이 아니다 — 아무것도 올라오지 않을 수 있다 | `{asked, why}` — `why` 는 `asked` · `open` · `skipped` · `recently` · `unheard` | `missing_fields`(`reason`) · `not_found`(`task_id`) · `precondition_failed`(파트나 프로젝트를 정할 수 없다) |

- `ask_owner` 의 kind 별 인자: `question` 은 `body`(필수) · `options=[]`. `choice` 는 `body`(= `payload.question`) · `options`(2개 이상, 각 `{id, label, detail, figure=""}`. `figure` 는 아래) · `recommended=""` · `fix_points=""`. `approval` 은 `what` · `why` · `if_not`(셋 다 필수) · `policy_ref=""`. 결재 종류로의 대응은 §1.5 다.
- 파일을 받는 인자는 셋이다 (`room_post` 의 `attachments[]` 는 S-6 확정. `options[].figure` 의 파일과 `evidence[].ref` 의 파일은 S-11 ② 확정 — 결재 id 를 전이 전에 서버가 정해 `attachments/<결재 id>/` 로 복사하고 전이가 실패하면 그 폴더를 지운다. `figure.path` 는 `kind = choice` 에서만 받는다). `room_post` 의 `attachments[]` 는 `{path, name=""}`. `ask_owner(kind = choice)` 의 `options[].figure` 는 `""`(없음) · `{path, name=""}`(그림 파일) · `{mermaid}`(Mermaid 원문) 중 하나. `task_request_review` 의 `evidence[].ref` 는 문자열이고 `kind` 가 `screenshot` · `log` · `file` 이면 파일 경로, `url` 이면 URL 이다. 파일 경로는 모두 §6.6 '직원의 첨부'의 검사를 거쳐 서버가 복사한다. 걸리면 `precondition_failed` 와 `failed[]`(`attachment_not_found` · `attachment_outside_workspace` · `attachment_too_large`)이고 글 · 결재 · 전이는 만들어지지 않는다. 저장되는 모양은 §5.3 · §5.4 · §6.6 이다.
- `task_request_review` 의 `precondition_failed` 는 하네스 게이트(§4.3 T6, S-11)와 줄 서 있는 작업, 그리고 위의 첨부 검사다. 결과의 `failed[]` 에 걸린 항목을 적는다 — 예: `structure_choice_missing`(feature 의 코드가 바뀌었는데 `choose` 로 결정된 구조 선택이 없다). 완료 기준의 표기가 빠졌거나 틀린 것은 `precondition_failed` 가 아니라 `missing_fields`(`fields = [checklist]`)다.
- `room_post` 의 `mentions` 는 그 방의 구성원인 직원이어야 한다. 작업방에서 직원을 멘션하면 `exchange_count` 를 올리고(§4.6), 한도에 닿으면 §4.6 대로 한다 (`working` 이면 T4, 그 밖은 §4.5 의 표). 자기 자신은 멘션할 수 없다.
- 작업방에서 멘션한 직원의 턴 (S-9 확정). 담당이면 그 글이 작업 런에 `engine.interject` 로 끼어든다 — 누구의 글인지와 owner 의 지시가 아님을 앞에 적는다. 작업이 `working` 일 때만이고, 런이 아직 없으면 넣은 것으로 본다 (그 런은 시작할 때 방을 읽는다). 그 밖의 구성원이면 읽기 전용 턴(`mode = read`, §9.5)을 넣는다. `queued[]` 는 턴이 들어간 직원 id 이고 그 수만큼 `exchange_count` 를 올린다. 들어가지 않은 직원은 `message` 에 적는다. 단체방 · 파트방의 멘션은 방의 줄에 턴을 넣고(§6.6 '단체방의 턴') `queued` 는 줄에 들어간 직원 id 다 — 세는 것은 §4.6 의 '단체방의 왕복 한도'다. 1:1 방 · 시스템 방의 멘션은 턴을 넣지 않고 `queued` 는 빈 배열이다.
- 단체방에서 멘션한 직원의 턴. 재직 중인 지금 구성원이면 그 방의 줄에 채팅 턴이 들어가고 — 줄 끝이 아니라 부른 직원의 턴 바로 다음이다, 빠른 판단 없이 (§6.6 '단체방의 턴') — `queued[]` 에 그 id 가 든다 — 턴은 부른 직원의 턴이 끝난 뒤에 돈다. 자기 자신과 구성원이 아닌 직원의 멘션은 글을 만들기 전에 `precondition_failed` 로 거부한다. 한도는 글을 쓸 때가 아니라 그 턴을 시작하기 직전에 본다 (§4.6): 한도에 닿아 있어도 글은 남고 `exchange_limit` 은 돌려주지 않는다 — `queued[]` 는 줄에 선 직원이지 턴이 돈 직원이 아니다. 퇴사한 구성원을 멘션하면 글은 남고 `queued[]` 에서 빠진다. 턴을 만들지 못하면 (§9.5) 그 방의 시스템 글이 이유를 말한다.
- `room_post` 의 `handoff` 는 파트 간 인계 형식이다 (H-5 확정, `studio/handover.py`). `null`(생략)이면 인계가 아닌 글이고, 값이 있으면 아래 두 형식 중 하나여야 한다. 맞지 않으면(형식 이름을 모름 · `format` 없음 · 표 없음 · 빈 표 · 필수 칸 없음 또는 공백 · 모르는 칸 · `{}`) `invalid_format` 과 이유(`message`, 맞는 형식의 한 줄 설명 포함)와 틀린 칸의 이름(`fields[]`, 예 `rows[0].done`)을 돌려주고 글을 만들지 않는다. 검사는 첨부 · 멘션 검사보다 앞, DB 를 건드리기 전이다 — 틀린 인계가 왕복 한도의 결재 같은 부작용을 남기지 않는다. 맞는 값은 고치지 않고 `messages.meta.handoff` 에 그대로 저장한다.

  | 형식 | 방향 | 표 | 행의 필수 칸 | 행의 선택 칸 |
  |---|---|---|---|---|
  | `spec_table` | 기획(문서) → 프로그램: 사양 표 | `rows` | `item` · `value` · `done` | `condition` · `exception` · `note` |
  | `data_items` | 프로그램 → 기획(세팅): 노출된 데이터 항목 목록 | `items` | `asset` · `field` · `type` | `default` · `note` |

  최상위는 `format` · 표 · `source`(선택, 사양이 있는 `Docs/` 경로)뿐이고 칸 값은 모두 글(문자열)이다. 필수 칸은 공백만 있어서는 안 된다. 보내는 직원의 파트와 방 종류는 검사하지 않는다 — 방향은 형식 정의와 파트 지침 템플릿(H-1)의 '인계' 절에만 적는다(사용자 정의 파트가 있다). 이미 지침이 있는 DB 의 파트에는 시작할 때 절만 덧붙인다 (§3.15). 검수로 넘기는 것은 이 형식이 아니라 `task_request_review` 의 패키지(S-12)다. 알려진 한계: `spec_table` 의 칸은 game-harness 의 game-design-doc 원본과 대조하지 못했다 — 칸 정의는 `handover.FORMATS` 한 곳이다. 멘션한 직원의 턴 넣기와 `exchange_count` 는 S-9 가 했다 (위).
- `task_progress` 는 작업이 `working` · `blocked` · `review` 일 때 받는다 (T4 · T6 뒤에도 런이 돈다).

**인사 전용** (호출자의 `department_id` 가 `hr` 이 아니면 `actor_not_allowed`)

| 도구 | 인자 | 하는 일 | 성공 반환 | 오류 |
|---|---|---|---|---|
| `employee_draft` | `draft_id=""` · `name` · `department_id` · `project_id=""` · `persona_md` · `guide_md` · `employee_yaml` · `first_message=""` · `part_name=""` · `part_job=""` · `part_relay=""` · `part_models=""` · `target_employee_id=""` · `file=""` | 초안을 만들거나 고친다. `target_employee_id` 가 있으면 기존 직원의 수정안이다 (`file` 과 그 본문만 쓴다). `part_name` 이 있으면 새 파트를 함께 제안한다 (아래) | `{draft_id, action, problems[]}` (새 파트면 `department_id` 도) | `missing_fields` · `not_found` · `precondition_failed` |
| `employee_submit` | `draft_id` · `reason` · `source_inbox_id=""` | 초안으로 `hire` 결재를 만든다 (`action` 은 `hire` 또는 `guide_amend`). 같은 초안의 `pending` 결재가 있으면 대체한다 (§5.5) | `{inbox_id}` | `not_found` · `precondition_failed` |
| `policy_draft` | `project_id` · `policy_md` · `reason` · `submit=False` · `source_inbox_id=""` | 정책 초안을 저장하고 지금 파일과의 차이를 돌려준다. `submit=True` 면 `hire` 결재(`action = policy`)를 만든다 | `{draft_id, diff, inbox_id}` | `not_found` · `missing_fields` |
| `hire_recommend` | `reason` · `role` · `model` · `effort` · `department_id=""` · `part_name=""` · `part_job=""` · `part_relay=""` · `part_models=""` · `project_id=""` · `task_ids=[]` · `signal_key=""` | 채용 권장(`hire_recommendation`, §5.3)을 owner 에게 올린다. 파트는 `department_id` 또는 `part_name` + `part_job` 중 하나다. 근거 `numbers`(그 파트의 재직 수 · 그 프로젝트의 배치 수)와 제목은 서버가 채운다. `project_id` 를 비우면 인사의 배치 프로젝트다. 올리면 인사의 1:1 방에 한 줄이 남는다 | `{inbox_id}` | `actor_not_allowed` · `missing_fields` · `precondition_failed` · `conflict`(같은 키가 결재를 기다린다) |

- 초안은 `runs/studio/drafts/<draft_id>.json` 에 둔다. id 는 `drf_<hex8>` 이다 (잠정, H-2). 제출하면 초안의 본문을 결재의 payload 에 복사하므로, 그 뒤에 초안을 고쳐도 올라간 결재는 바뀌지 않는다.
- `employee_draft` 의 `problems[]` 는 서버가 찾은 문제다 (이름 중복, 없는 파트, `employee.yaml` 검사 실패 — §9). 문제가 있어도 초안은 저장되고, `employee_submit` 이 `precondition_failed` 로 막는다. 원소는 `{code, message}` 이고 코드는 `name_taken` · `department_not_found` · `hr_exists` · `hr_has_project` · `project_not_found`, 새 파트 제안의 `part_job_missing` · `part_name_taken` · `part_id_taken` · `part_models_invalid`(그리고 `part_relay` · `part_models` 가 §9.4 에 걸린 코드 — 없는 릴레이는 `relay_unknown`), 그리고 `employee.yaml` 검사의 열한 코드(§9.4), 마지막으로 채용 초안의 `guide_md` 에 '## 결재 기준' 절이 없거나(`approval_section_missing`) 파트 틀의 빈 칸 그대로인(`approval_section_empty`; `- 묻고 한다…:` · `- 알아서 한다:` 줄이 내용 없이 끝난 경우 — 해당이 없으면 `없음`이라고 적는다) 때의 두 코드다 (H-6; `employees.approval_problems`. 수정안 `guide_amend` 의 `GUIDE.md` 초안에도 같은 검사를 한다 — 대상이 인사 직원이면 빼고, 승인 때는 다시 검사하지 않는다) (S-6 의 `employee_yaml_invalid` 는 S-7 이 `yaml_invalid` 로 바꿨다. `drafts.yaml_problems` 가 `employees.check` 를 부른다). `employee_submit` 은 지금의 DB 와 파일로 다시 검사하고 걸린 코드를 `failed[]` 에 넣는다 — 수정안이면 `target_not_found` · `file_changed`(초안 뒤에 그 파일이 바뀌었다)가 더 있다.
- `draft_id` 를 주고 `employee_draft` 를 부르면 빈 인자는 저장된 값을 유지한다 — 한 항목만 고쳐 다시 낼 수 있다. 채용 초안을 수정안으로(또는 다른 직원의 수정안으로) 바꾸는 호출은 `precondition_failed` 다. 수정안의 `base_sha256` · `diff` 는 초안을 저장할 때의 파일로 적는다 (S-6).
- 새 파트 제안 (T3 확정): 인사는 어느 파트로든 뽑는다 — `department_id` 는 '채용에 쓰는 값'(§8.4)의 파트 어느 것이든 되고 아트와 사용자 파트도 거기 나온다. 맞는 파트가 없으면 `part_name` · `part_job` 을 적어 새 파트를 함께 제안한다(`part_relay` · `part_models` 는 정했을 때만). 이때 `department_id` 를 적으면 `precondition_failed` 다 — 파트의 id(`dep_<hex8>`)는 초안을 처음 저장할 때 서버가 정해 돌려주고 같은 초안에서는 바뀌지 않는다. 새 파트를 제안한 초안은 기존 파트로 채용하는 초안으로 되돌릴 수 없다 — 그때는 `draft_id` 를 비워 새 초안을 만든다(거부 문구가 그렇게 안내한다). `employee_yaml` 의 `department` 에는 `{department_id}` 를 적으면 서버가 그 id 로 바꿔 저장한다(owner 는 결재에서 바뀐 yaml 을 본다). `problems[]` 는 새 파트를 지금의 DB 로 검사한다 — 이름 중복(보관 포함)과 없는 릴레이는 초안 단계에서 걸러진다. `employee_submit` 의 제목 · payload 는 §5.5 다. 승인하면 파트 · 파트방 · 직원 · 1:1 방이 한 트랜잭션에 생기고 반려하면 아무것도 생기지 않는다.
- 정책 초안은 프로젝트마다 하나다. id 가 프로젝트 id 에서 정해져(`drafts.policy_id`), 다시 올리면 앞의 `pending` 결재가 대체된다 (§5.5. S-6).
- S-6 은 정책 도구를 `policy_draft` 하나만 적는다. 제출 도구가 따로 없어 `submit` 인자로 받는다. `[§12]`
- 인사 전용 도구와 `staff_request` 는 `end_turn` 이 `false` 다. 인사의 채팅 턴은 답을 방에 남기고 스스로 끝난다. 도구는 12개가 아니라 14개다 — `hire_recommend` · `staff_request` 는 T5 가 더했다. `hire_recommend` 가 `conflict` 를 돌려줄 때는 `inbox` 에 열린 결재가 실리지 않고 `code` 와 문장만 간다(§8.3).
- 판단 턴 (T5): 계기가 판단 요청을 통과하면 서버가 인사의 1:1 방에 시스템 글(`meta.event = staffing.signal`, `key` · `trigger`)을 올리고 인사의 채팅 턴을 넣는다. 글은 계기의 종류 · 파트 · 프로젝트 · 관련 작업 · 수치, 파트별 재직 수와 그 프로젝트 배치 수, 시작 전 작업 수와 '필요하면 `hire_recommend(signal_key=)` 로 올리고 아니면 아무것도 올리지 마라'를 담는다. 새 글이 서버의 시스템 글뿐이고 그중에 계기가 있는 턴은 조용한 턴이다 — output 이 비어도 '답하지 못했다'를 쓰지 않는다. 인사는 읽기 전용(`mcp__studio` 뿐)이라 파일을 고치지 못한다.
- **채용에 쓰는 값** (H-2 확정). 파트 절에는 보관되지 않은 파트 가운데 `hr` 을 뺀 전부가 나온다 — 아트(T3)와 owner 의 사용자 파트도 기본 릴레이 · MCP · 모델 · GUIDE 틀과 함께 나온다(`tests/test_studio_parts.py`). 인사는 프로젝트 · 파트 · 모델을 나열하는 REST(`GET /studio/projects` · `/departments` · `/models`)를 부를 수 없다 — owner 의 경로다. 그래서 인사 직원의 채팅 턴 프롬프트 끝에 `===== 채용에 쓰는 값 =====` 절을 싣는다 (`employees.hiring_notes`, `chat.py` 의 `_make` · `_hiring`): 보관되지 않은 프로젝트의 id · 이름(없으면 `project_id` 를 비우라는 한 줄), `hr` 을 뺀 파트의 id · 이름 · `default_models.chat`(비어 있으면 '기본값 없음'), Claude Code 러너의 모델과 모델별 노력치(`GET /studio/models` 와 같은 목록). 같은 파트마다 `employee.yaml` 에 적지 않으면 따르는 값(`default_relay` · `default_mcp` · `default_models` 의 `work` · `cap`) 한 줄과 `guide_template` 의 본문(비어 있으면 '틀 없음 — 범용 틀')도 싣는다 (H-1 확정) — 인사는 그 파트의 틀을 면담 내용으로 채워 `guide_md` 로 내고, 네 값은 owner 가 다르게 정했을 때만 적는다. `employee_draft` 는 빈 `guide_md` 를 틀로 채우지 않는다 — 빈 틀이 그대로 결재에 오르지 않게 한다. 모델 목록이 비었거나 읽지 못하면 그 줄들만 빠지고 턴은 그대로 만들어진다. 프롬프트의 다른 절은 대화를 새로 여는 턴에만 실리지만(§9.5) 이 절은 이어 가는 턴에도 새 글 뒤에 다시 싣는다 (H-2 확정). 정책과 결재 규칙은 이어 가는 턴에 정책 글이 바뀐 때만 새 글 뒤에 다시 싣는다 (`chat.py` 의 `_policy_note`; 비교용 기록은 메모리에 있어 서버를 다시 시작하면 한 번 더 간다) (H-6 확정). 직원 자신의 `GUIDE.md` 도 같다 — 대화를 연 뒤 바뀌었으면(승인된 `guide_amend` 등) 이어 가는 턴에 지금 본문을 한 번 싣는다 (`_guide_note`; 서버를 다시 시작한 뒤 첫 이어 가는 턴에는 비교할 앞 값이 없어 싣지 않는다). 절은 턴마다 store 와 카탈로그에서 새로 만들므로, 대화 도중 만든 프로젝트가 다음 턴에 보인다. 작업방의 읽기 턴(`mode` 가 `read`)에는 싣지 않는다 — 채용 초안을 쓰는 턴이 아니다. 그래도 어긋난 값은 `employee_draft` 의 `problems[]`(`project_not_found` · `department_not_found` 등)가 알려 준다. 인사가 아닌 직원의 턴과 작업 런에는 이 절이 없다.

### 8.5 도구별 권한

| 도구 | 조건 (지금의 DB 로 본다) |
|---|---|
| `task_request_review` · `task_progress` | 호출자가 지금 작업의 담당이고, 토큰의 `run` 이 그 작업의 `tasks.run_id` 다. 담당이라도 다른 방의 채팅 턴에서는 못 올린다 |
| `ask_owner` | 지금 작업이 있고 호출자가 그 작업방의 구성원이다 |
| `room_post` | `task` 방 · `group` 방 · `department` 방 = 구성원(`left_at` 이 비어 있는 행), `dm` 방 = 자기 1:1 방, `system` 방 = 인사만 (일일 보고, S-13) |
| `task_propose` · `followup_propose` | 재직 중이고 휴직이 아니다. 그 프로젝트에 배치돼 있다 (인사는 예외) |
| `task_get` · `task_list` · `employee_list` | 호출자가 배치된 프로젝트의 것. 인사는 전체 |
| `employee_draft` · `employee_submit` · `policy_draft` · `hire_recommend` | 호출자의 파트가 `hr` |
| `staff_request` | 재직 중인 직원 누구나. 파트나 프로젝트를 정할 수 없으면(인사 파트 포함) `precondition_failed` |

작업방에서 글을 쓰는 것(`room_post`)은 구성원 모두가 한다. S-9 의 "쓰기 권한은 담당 1명"은 파일을 고치는 권한이고(§3.7), 그것은 런의 워크스페이스 방식으로 갈린다 — 담당의 작업 런만 쓰는 워크스페이스를 갖는다.

## 9. employee.yaml

근거: 전제 1, S-7, S-8, H-1 · H-2 · H-3 · H-4. 값을 런에 넘기는 자리는 §11.2, 직원의 직접 쓰기를 막는 쪽은 §10 이다.

### 9.1 직원 폴더의 파일 넷

`runs/studio/employees/<employee_id>/` 에 파일 넷이 있다 (§2.2).

| 파일 | 내용 | 프롬프트에 | 쓰는 길 |
|---|---|---|---|
| `PERSONA.md` | 정체성 — 이름, 맡은 일, 말투, 일하는 방식 | 들어간다 | `hire` 승인 · `PUT /studio/employees/{id}/files/{name}` · `guide_amend` 승인 · `revert` |
| `GUIDE.md` | 파트 지침과 결재 기준 절. 채용 때 `departments.guide_template` 에서 시작한다 (H-1, H-2) | 들어간다 | 위와 같다 |
| `MEMORY.md` | 배운 점. 검수 결과에서 서버가 뽑아 쌓는다 (H-4). 채용 때는 빈 파일이다 (§5.5) | 들어간다 | 서버(H-4) · `PUT …/files/MEMORY.md`(owner) · `revert`. `guide_amend` 의 대상이 아니다 (§5.3) |
| `employee.yaml` | 설정 — 파트 · 릴레이 · MCP · 모델 · 노력치 · 상한 | 들어가지 않는다 | `PERSONA.md` 와 같다. 쓸 때마다 §9.4 의 검사를 거친다 |

- 프롬프트에 얹는 순서는 S-7 의 문장 그대로다: `PERSONA.md` → `GUIDE.md` → `MEMORY.md` → 프로젝트 정책(`POLICY.md`, §2.2) → 하네스 → 방의 최근 글 요약. 작업 런은 여기에 작업의 제목 · 사유 · 명세 · 완료 기준이 붙는다 (§11.2 의 `goal`. 조립 방식은 S-8). 인사 직원의 채팅 턴은 이 다섯 뒤에 '채용에 쓰는 값' 절이 하나 더 붙는다 (§8.4 인사 전용, H-2). 이어 가는 턴에는 새 글 뒤에 이 절만 다시 붙고, 작업방의 읽기 턴에는 붙지 않는다. 빈 절은 뺀다. 방의 최근 글 요약은 최근 20개를 `작성자: 본문`(글당 200자) 줄로 나열한 것이고 모델을 부르지 않는다 (S-7 확정). '하네스' 절은 정책 뒤, 결재 규칙 앞이다 — 채팅 · 읽기 · 작업 턴의 대화를 새로 시작하는 프롬프트에만 실리고 이어 가는 턴에는 다시 붙지 않는다. 하네스의 README 첫 문단(한 줄로 이어 170자까지)과 `skills/*/SKILL.md` 앞머리의 `이름 — 언제` 한 줄씩(설명 첫 줄의 첫 문장, 90자까지)이고 UTF-8 2400바이트를 넘으면 앞에서부터 줄 단위로 자른다 (`harness_root.section`). 처음에는 설명 줄 전체 · 1500바이트였는데, 이 PC 의 하네스에서 스킬 7개 중 2개만 들어가서 (2026-10-17) 지금 값으로 바꿨다 — 지금 하네스로 약 1500바이트에 7개가 다 들어간다. 하네스 루트는 설정 `studio_harness_root` → 없으면 `~/.claude/skills/game-onboard` 정션의 두 단계 위(`skills/` 가 있어야 하고 복사본이면 아니다) → 없으면 None(절이 없고 예외도 없다)이다 (`harness_root.resolve_root`). 프로젝트가 정해진 턴 · 작업 런(배치된 직원)에는 정책(과 하네스) 절 뒤, 방의 최근 글 앞에 '결재 규칙' 절이 하나 더 붙는다 — `POLICY.md` 가 없어도 붙는다 (H-6; `employees.APPROVAL_RULE`): 바꾸기 전에 정책과 GUIDE.md 의 결재 기준에 비춰 보고, 해당하면 바꾸지 말고 `ask_owner`(kind=approval)로 무엇을 · 왜 · 안 하면 어떻게 되는지를 올리고 턴을 끝낸다. 프로젝트를 가져오거나 만들면 인사의 1:1 방에 `[정책 면담]` 시스템 글(`meta.event = policy_interview`)이 올라오고 인사의 턴이 들어간다 (`policy.begin_interview`. 가져온 프로젝트면 규칙 파일(CLAUDE.md 등)의 내용이 글에 실린다). 이미 있는 인사의 GUIDE.md 에는 서버를 시작할 때 '## 프로젝트 정책' 절만 덧붙는다 (`employees.add_policy_talk`; 고친 글은 그대로).
- `employee.yaml` 은 서버만 읽는다. 값은 런을 만들 때 인자로 들어가고(§9.5) 직원에게 글로 보이지 않는다.
- `MEMORY.md` 의 상한은 UTF-8 로 4096바이트다 (H-4 의 "4KB"). 서버가 쌓다가 넘게 되면 쓰기 전에 합친다 — 합치는 방법은 아래 H-4 확정이다. `PUT` 으로 넘는 본문이 오면 `precondition_failed` 이고 `failed[]` 에 `memory_too_large` 를 넣는다. 그래도 파일이 넘어 있으면(손으로 고친 경우) 프롬프트에는 뒤에서부터 줄 단위로 4096바이트까지만 얹는다 (H-4 확정). 마지막 줄 하나가 4096바이트를 넘으면 그 줄의 뒤쪽 4096바이트만 얹는다 (S-7).
- 메모리 쌓기 (H-4 확정, `studio/memory.py`): `final_review` 의 결정에 코멘트가 있으면(재작업은 코멘트가 필수다. 사유 없는 통과 · 되돌리기는 모델을 부르지 않는다) 결정이 커밋된 뒤 서버가 `Config.studio_memory_model`(기본 `haiku`, 빈 값이면 하지 않는다)을 1회 불러 '다음에 다르게 할 것'을 뽑는다. 재료는 작업 제목 · 결정 · 코멘트 · 완료 기준 · 지금 항목이고, 답에서 줄마다 불릿을 떼고 `없음` 과 이미 있는 내용을 빼고 최대 3줄 · 줄당 200자로 자른다. 이 호출은 `api.learn_later` 의 데몬 스레드에서 돌고 결정의 응답을 기다리게 하지 않는다 — 재작업으로 곧바로 이어지는 턴에는 새 줄이 안 실릴 수 있고 그다음 턴부터 실린다. 호출이 실패해도 결정과 파일은 그대로다. 항목은 한 줄이다: `- <내용> (출처: <작업 id> · YYYY-MM-DD)`, 합쳐진 줄의 출처는 `합침` 이다. `- ` 로 시작하지 않는 줄은 항목이 아니고, 손으로 쓴 `- ` 줄은 출처 없는 항목이다. 항목 id 는 그 줄(양끝 공백 제외)의 SHA-256 앞 12자다. 쓰기는 직원 폴더 lock 안에서 읽기 → 덧붙이기 → 쓰기 → `memory: learn <작업 id>` 커밋 → `employee.updated`(`change = file`) 순이다(커밋이 실패하면 로그만 남기고 파일은 둔다). 덧붙여 4096바이트를 넘게 되면 lock 밖에서 같은 모델에 합치기를 시켜 2048바이트 이하를 요청하고(항목이 아닌 줄은 맨 위에 그대로 둔다), 파일이 그새 바뀌었거나 합치기가 실패했거나 여전히 넘으면 오래된 줄(맨 위)부터 버려 상한 안에 맞춘다. 추출 · 합치기 호출의 사용량(토큰 · 비용)은 usage DB 에 적지 않는다.
- `GUIDE.md` 의 크기는 서버가 검사하지 않는다. 패키지에 든 인사 원본(`relay_agent/studio/hr/GUIDE.md`)만 테스트가 5632바이트 이하로 붙든다(서버 상한이 아니라 `tests/test_studio_employees.py` 의 `HR_GUIDE_BUDGET` 이다) — 턴마다 프롬프트에 들어가기 때문이다.
- 네 파일은 턴을 시작할 때마다 읽는다. 고친 값은 다음 턴 · 다음 런부터 들고, 도는 런은 바뀌지 않는다 (S-7 확정).

### 9.2 키

**칸**은 모델 설정 하나다: `{provider, model, effort}`. 셋 다 선택이고 `stage_models` 의 단계별 값과 같은 모양이다 (`pipeline.py` 561–580). 빈 값은 "릴레이 파일의 값 그대로"다.

| 키 | 형 | 없을 때 | 뜻 |
|---|---|---|---|
| `version` | 정수 | 거부 | 형식 버전. `1` |
| `department` | 문자열 | 거부 | 파트 id. `employees.department_id` 의 사본이다 (§9.6) |
| `relay` | 문자열 | `departments.default_relay` | 작업 런의 릴레이. `relays/<이름>.yaml` 의 이름이거나 `auto` (§9.5) |
| `mcp` | 문자열 목록 | `departments.default_mcp_json` | 작업 런에 붙일 MCP 서버 이름. MCP 레지스트리(§11.4)에 있는 이름이어야 한다. `studio` 는 서버가 항상 붙이므로 적지 않는다 |
| `chat_mcp` | 문자열 목록 | `[]` | 채팅 턴 · 읽기 전용 턴에 붙일 것. `mcp` 의 부분집합이다 |
| `chat` | 칸 | `default_models_json` 의 `chat` | 채팅 턴 · 읽기 전용 턴의 모델 · 노력치 |
| `work` | `{단계 이름 또는 "*": 칸}` | `default_models_json` 의 `work` | 작업 런의 단계별 모델 · 노력치. `*` 는 이름을 적지 않은 단계 전부다 |
| `cap` | `{model, effort}` | `default_models_json` 의 `cap` → 설정 (§9.5) | 이 직원의 상한. `model` 은 `MODEL_ORDER`, `effort` 는 `EFFORT_ORDER` 의 값이다 (`pipeline.py` 60–61) |

- 키는 이 여덟뿐이다. 이름 · 배치 · 상태는 `employees` 테이블이 가진다 (§2.3, §3.5).
- `departments.default_models_json`(§3.4)은 `{chat, work, cap}` 이고 모양이 위와 같다. 없는 키를 채우는 것은 읽을 때이고 파일에 되쓰지 않는다 — 파트 기본값을 고치면 그 키를 적지 않은 직원이 따라 바뀐다.
- `mcp` 와 `chat_mcp` 를 나눈 이유: S-7 의 "unreal MCP 는 기본 끔"을 키로 드러낸다. `chat_mcp` 의 기본이 빈 목록이라 `mcp: [unreal]` 인 직원도 채팅에서는 unreal 이 붙지 않는다. `departments.default_mcp_json` 의 평평한 목록은 그대로 둔다. CAMPAIGN.md 는 MCP 목록을 하나로만 적는다. `[§12]`
- "직원이 필요하다고 하면 그 턴만 켠다"(S-7)의 방법 — 누가 요청하고 다음 턴에 어떻게 붙이는가 — 은 정하지 않았고 구현도 없다 (미구현. 받을 작업은 H-3 후보 — CAMPAIGN.md 의 H-3 명세에는 직접 없다. 그 작업 계획에서 범위에 넣을지 정한다). S-7 의 채팅 턴은 `chat_mcp` 와 `studio` 만 붙인다 (`studio/chat.py`, §11.2 의 `mcp_only`). 켤 수 있는 범위는 `mcp` 안이다.
- 읽기 전용 턴(`mode = read`, §7.4)은 `chat` 칸과 `chat_mcp` 를 쓴다 (S-9 확정 — `studio/chat.py` 가 채팅 턴과 같은 값으로 만든다).
- `cap` 은 CAMPAIGN.md 에 없는 키다. S-8 은 상한을 `model_cap` / `effort_cap` 이라고만 적고 어디서 오는지 적지 않는다. 직원마다 맡는 일이 달라 채용 면담(H-2)에서 정할 값으로 봤다. `[§12]`

### 9.3 예시

프로그램 파트 직원이다. `build` 와 `review` 단계만 따로 정하고 나머지 단계는 `*` 를 따른다.

```yaml
version: 1
department: program          # employees.department_id 와 같아야 한다
relay: auto                  # 생략하면 departments.default_relay
mcp: [unreal]                # 작업 런에 붙는다. studio 는 적지 않는다
chat_mcp: []                 # 채팅에서는 unreal 을 붙이지 않는다 (S-7)
chat:
  model: sonnet
  effort: medium
work:
  "*":    {model: sonnet, effort: high}
  build:  {model: opus, effort: high}
  review: {model: opus, effort: xhigh}
cap:
  model: opus                # fable 로 올리지 않는다
  effort: xhigh
```

- `build` · `review` 는 `relays/default.yaml` · `relays/studio-task.yaml` 에 있는 단계 이름이다 (둘 다 `scout` · `plan` · `build` · `review`).
- 예시의 모델과 노력치 조합이 지금 카탈로그에서 통하는지는 확인하지 않았다. 파트별 기본값은 H-1 이 정한다.

### 9.4 검사

`employee.yaml` 을 쓰는 모든 길(§9.6)과 `employee_draft` 의 `problems[]`(§8.4)가 같은 검사를 쓴다. 걸리면 REST 는 `precondition_failed` 이고 걸린 코드를 모두 `failed[]` 에 넣는다 (§6.3). 코드 이름은 S-7 이 아래 열하나로 확정했다 (`studio/employees.py` 의 `CODES`). 1 에 걸리면 `yaml_invalid` 하나만 낸다. 아니면 걸린 검사를 모두, 코드당 한 번, 표의 번호순으로 낸다. 형이 틀린 값은 그 키의 검사에 걸린 것으로 본다 (`relay: 3` → `relay_unknown`). `chat` · `work` · 칸이 매핑이 아니거나 칸의 `model` 이 문자열이 아니면 `yaml_invalid` 다. 값 없이 적은 키(`mcp:`)는 적지 않은 키와 같다.

| # | 검사 | `failed[]` |
|---|---|---|
| 1 | UTF-8 YAML 이고 최상위가 매핑이다 | `yaml_invalid` |
| 2 | 모르는 키가 없다 (최상위, 칸, `cap` 안) | `unknown_key` |
| 3 | `version` 이 `1` 이다 | `version_unsupported` |
| 4 | `department` 가 `employees.department_id` 와 같다. 채용 초안이면 `draft.department_id` 와 같다 | `department_mismatch` |
| 5 | `relay` 가 `auto` 이거나 `Config.relays_dir`(`config.py` 21)에 그 이름의 릴레이 파일이 있다 | `relay_unknown` |
| 6 | `mcp` 의 이름이 모두 MCP 레지스트리에 있고 `studio` 가 없다 | `mcp_unknown` |
| 7 | `chat_mcp` 가 `mcp` 의 부분집합이다 (`mcp` 가 없으면 파트 기본값으로 채운 뒤 본다) | `chat_mcp_not_subset` |
| 8 | 칸의 `provider` 가 비었거나 Claude Code 러너다. 가드 훅을 걸 수 있는 러너가 지금은 그것뿐이다 (§10.8 의 9. S-10 확정 — `employees.py` 의 `guarded`) `[§12]` | `provider_unguarded` |
| 9 | 칸의 `effort` 가 `EFFORT_ORDER` 의 값이고, `providers.efforts_for(provider, model)` 이 목록을 주면 그 안에 있다 | `effort_unsupported` |
| 10 | `cap.model` 이 `MODEL_ORDER`, `cap.effort` 가 `EFFORT_ORDER` 의 값이다 | `cap_invalid` |
| 11 | 이 파일에 `cap` 이 있으면 `chat` · `work` 의 칸이 그 이하다 (비교 방법은 §9.5 의 6) | `over_cap` |

- 저장 때 보지 않는 것 둘. (가) `work` 의 단계 이름 — 릴레이가 `auto` 이거나 작업마다 `tasks.relay` 로 바뀔 수 있어 어떤 단계가 있을지 모른다. 실행 때 본다 (§9.5 의 4). (나) 칸의 `model` 을 지금 쓸 수 있는가 — `GET /models` 의 목록은 사용량 한도와 설치 상태로 그때그때 달라진다 (§11.3). 저장을 그 순간의 상태에 묶지 않고 실행 때 본다 (§9.5 의 5).
- 8 은 CAMPAIGN.md 에 없는 제한이다. S-8 은 고를 수 있는 값을 `GET /models` 의 목록 전부로 적는다. 비어 있는 `provider` 는 저장 때 통과하고, 릴레이 파일이 단계에 적은 값까지 넣어 실행 때 다시 본다 (§9.5 의 5). 이번만 덮어쓴 칸과 `/studio/models` 도 같은 제한을 따른다 (§6.4). owner 확정 (§12.6 의 12): 직원의 모델은 Claude Code 로만 한정한다. 풀려면 다른 러너에 가드를 거는 방법이 먼저 있어야 한다.
- `hire` 승인에서 걸리면 결정하지 않고 결재는 `pending` 으로 둔다 (§5.5 의 표에서 (1)이 실패한 경우와 같다). `employee_submit` 은 그 전에 `precondition_failed` 로 막는다 (§8.4).

### 9.5 값이 정해지는 순서

작업 런을 만들 때마다 — §4.3 T3, 그리고 런이 `done` 으로 끝난 뒤 새 런을 만드는 §4.7 — 서버는 아래 순서로 값을 정한다.

| # | 걸음 | 내용 |
|---|---|---|
| 1 | 릴레이 | `tasks.relay` → 없으면 `employee.yaml` 의 `relay` → 없으면 `departments.default_relay`. 셋 다 없으면 런을 만들지 못한다 (§4.3 T3 의 실패 처리). 정해진 릴레이가 `auto` 면 런을 만드는 순간에 엔진의 공개 라우팅 메서드로 릴레이 하나를 고르고, 2–7 은 고른 릴레이로 한다 (덧붙임의 `auto` 릴레이. S-8 확정) |
| 2 | 기본 칸 | `work` 를 그 릴레이의 단계에 펼친다. 단계 이름 키가 있으면 그 칸, 없으면 `*` 의 칸, 둘 다 없으면 칸 없음(릴레이 파일의 값) |
| 3 | 이번만 덮어쓰기 | `tasks.stage_models_json`(`start` 의 `stage_models`, §6.4)의 단계별 칸이 2 의 칸을 칸째 바꾼다. 키 단위로 섞지 않는다 |
| 4 | 단계 이름 | 릴레이에 없는 단계의 칸은, 덮어쓴 것이면 `start` 를 거부하고(`unknown_stage`) `employee.yaml` 것이면 버린다. 버리지 않고 넘기면 엔진이 `ValueError` 로 거부한다 (`pipeline.py` 564–565). `auto` 를 그대로 `create` 에 넘기면 엔진은 고른 릴레이에 없는 키를 오류 없이 버린다 (550–551). studio 는 먼저 고른 뒤 그 릴레이의 경로로 `create` 를 부르므로 564–565 의 검사가 그대로 선다 |
| 5 | 쓸 수 있는가 | (가) 가드를 걸 수 있다 — 릴레이의 단계마다 쓰일 provider 가 Claude Code 다. Claude Code 인지는 그 provider 의 `kind` 가 `claude_cli` 인지로 본다 (`providers.py` 28 · 106. 내장 `claude` 가 그렇다). 쓰일 provider 는 3 을 지난 칸의 `provider` 이고, 비었거나 칸이 없으면 릴레이 파일이 그 단계에 적은 값이다 (`pipeline.py` 566). 하나라도 다른 AI 면 `start` 를 거부한다(`provider_unguarded`) — 이번만 덮어쓴 칸이든 `employee.yaml` 의 칸이든 릴레이 파일의 단계 자체든 같다 (§10.8 의 9). (나) 모델을 쓸 수 있다 — 칸의 모델이 `GET /studio/models` 의 목록에 있고 노력치가 그 모델의 `efforts` 에 있다 (S-8, §6.4). 덮어쓴 칸이 걸리면 `start` 를 거부한다(`model_unavailable` · `effort_unsupported`). `employee.yaml` 의 칸은 그대로 넘기고 엔진의 검사(`check_choice` · `efforts_for`, 567–577)에 맡긴다 |
| 6 | 상한 | 아래 |
| 7 | `create` | `stage_models` = 6 을 지난 칸들, `model_cap` · `effort_cap` = 6 의 상한, `mcp` = `employee.yaml` 의 `mcp` + `studio` (§11.2). 서버 코드를 품은 프로젝트면 `copy` 고정 인자를 함께 넘긴다 (§4.3 T3). 엔진이 거부하면 §4.3 T3 의 실패 처리를 따른다 |

한 줄로: 릴레이 → `work` → 이번만 덮어쓰기 → 단계 이름 → 쓸 수 있는가 → 상한 → `create`.

**상한 (6).**

- 어디서 오는가: `employee.yaml` 의 `cap` → 없으면 `departments.default_models_json` 의 `cap` → 없으면 `Config.studio_model_cap` · `studio_effort_cap`(S-8 확정. `employees.config_cap` 이 읽어 작업 런과 채팅 턴에 같이 넘긴다) → 없으면 상한 없음. `model` 과 `effort` 를 따로 찾는다.
- 비교: 모델은 `ProviderRegistry.tier(model)` 을 `MODEL_ORDER` 순서로, 노력치는 `EFFORT_ORDER` 순서로 본다. 엔진의 `cap_model` · `cap_effort`(`pipeline.py` 64–76)와 같은 규칙이다 — `mythos` 는 `fable` 로 보고, 순서에 없는 모델(다른 AI 의 모델)은 상한에 걸리지 않는다.
- 이번만 덮어쓴 칸이 넘으면 `start` 를 `precondition_failed` 로 거부한다 (`model_over_cap` · `effort_over_cap`. §6.4). 낮춰서 받지 않는다 — owner 가 고른 값이 말없이 바뀌면 안 된다.
- `employee.yaml` 의 칸(파트 기본값에서 채운 칸 포함)이 넘으면 상한 값으로 낮춘다. 파일은 고치지 않는다. 파일 자신의 `cap` 은 저장 때 §9.4 의 11 이 봤으므로, 여기서 낮아지는 것은 상한이 파트 기본값이나 설정에서 온 경우다.
- 낮춘 모델이 `haiku` 면 노력치를 뺀다. 엔진과 같다 (1628–1629).
- 엔진에도 같은 상한을 넘긴다. 엔진은 칸이 없는 단계의 릴레이 기본값과 재시도 값에만 상한을 걸고(1623–1627), 칸이 있는 단계는 studio 가 위에서 본 값이 그대로 간다 (§11.2).

**덧붙임.**

- 이번만 덮어쓰기는 그 작업에 남는다. `tasks.stage_models_json` 에 있으므로 재작업(T8)과 `done` 뒤의 새 런에도 같은 값이 들어간다. `employee.yaml` 에는 되쓰지 않는다. `start` 를 `stage_models` 없이 부르면 저장된 값을 그대로 두고, `{}` 를 주면 지운다 (S-8 확정).
- 두 번 본다. `start` 요청 때 덮어쓴 칸을 3–6 으로 검사해 거부할 것을 거부하고, 슬롯을 얻어 런을 만드는 순간에 1–7 을 다시 한다 — 그 사이에 `employee.yaml` 이나 상한이 바뀔 수 있다. 그때 덮어쓴 칸이 걸리거나 5 의 (가)에 걸리면, 낮추거나 그대로 넘기지 않고 런을 만들지 않는다 (§4.3 T3 의 실패 처리. S-8 확정). 릴레이가 `auto` 면 `start` 요청 때는 릴레이를 고르지 않는다. 그때 덮어쓴 칸은 칸 자신의 `provider`(5 의 가) · 5 의 (나) · 6 만 본다. 4 와 릴레이 단계의 5 (가)는 런을 만드는 순간에 보고, 걸리면 같은 방식으로 런을 만들지 않는다.
- `auto` 릴레이 (S-8 확정). 단계 이름을 `create` 전에 알 수 없으므로, studio 가 먼저 고른 뒤 고른 릴레이로 `create` 를 부른다.
  - 확인한 것 (읽음). `relays/auto.yaml` 은 단계 없이 `router`(`model` · `candidates` · `exclude` · `fallback`, 6–10행)만 가진다. 릴레이를 고르는 일은 `create` 안에서 일어난다 (`pipeline.py` 543–551). 고르는 `_route`(634–654)는 비공개이고 `create` 만 부른다. 모델 호출이라 두 번 부르면 답이 다를 수 있다.
  - 엔진에 공개 라우팅 메서드를 둔다 (`RelayEngine.route`, S-8 확정. §11.2). studio 는 슬롯을 얻어 런을 만드는 순간에 한 번 부르고, 돌려받은 릴레이로 2–7 을 한 뒤 그 경로로 `create` 를 부른다. `create` 는 라우터가 없는 릴레이를 받으므로 다시 고르지 않는다. 2 · 4 · 5 를 `create` 안의 콜백으로 하는 길은 쓰지 않는다 — 엔진이 studio 의 검사를 알아야 한다.
  - 후보에서 미리 뺀다. 릴레이 파일이 Claude Code 가 아닌 provider 를 적은 단계를 하나라도 가진 릴레이는 제외 목록에 넣어 넘긴다 (`router.candidates` 의 `exclude`, `router.py` 41). 고른 뒤에도 5 의 (가)를 다시 본다 — 칸이 덮은 값까지 보는 것은 그때다.
  - 고를 수 없을 때 (`router.py` 104–122). 후보가 하나면 모델을 부르지 않고 그것을 고른다(110–111). 라우터 AI 가 없거나, 호출이 실패하거나, 답에서 후보 이름을 찾지 못하면 `fallback`(`quick`)을 고르고 예외는 올리지 않는다(112–121). `fallback` 이 후보에 없으면 — 제외됐거나 `needs_writes` 로 걸러졌으면 — 파일 이름순 첫 후보가 대신한다(108–109 · 40). 후보가 하나도 없으면 `ValueError` 다 (`pipeline.py` 641–642) — §4.3 T3 의 실패 처리를 따른다.
  - 새 런마다 다시 고른다. 재작업(T8)과 `done` 뒤의 새 런(§4.7)은 릴레이가 달라질 수 있다. 같은 런을 잇는 때에는 고르지 않는다 — 런이 고른 릴레이의 경로를 가진다 (`RunState.relay`, 548 · 592).
  - 기록. `create` 안에서 고르면 라우터 사용량을 `-:router` 로 적고 이벤트 `relay_routed`(`relay` · `reason` · `by` · `router` · `cost_usd`)를 남긴다 (620–625). 밖에서 고르면 둘 다 빠지므로, 공개 메서드가 돌려준 값을 `create` 에 넘겨 같은 기록을 남긴다 (`create` 의 `routed`, S-8 확정. §11.2).
- `mcp` 는 더하는 목록이지 거르는 목록이 아니다 (읽음: `pipeline.py` 446–455 `project_context` · 469–503 `_run_mcp_names` · `_attached_mcp` · 602–603 `create` 의 `live_mcp` · 1721 `stage_mcp`). 런에 붙는 MCP 는 셋의 합이다 — 릴레이 단계가 적은 `mcp`, `create` 의 `mcp` 인자(`None` 이면 저장소 설정의 `mcp`), 프로젝트 폴더에서 찾은 MCP. 셋째는 `mcp` 인자와 무관하게 붙는다 (`_attached_mcp` 491). 빠지는 것은 저장소 설정의 `project_context` 가 꺼져 있거나(448–449 · 476) 그 단계에 도구가 없을 때(1721)뿐이다 — 앞의 경우에도 `studio` 를 고른 런에는 그 런이 고른 레지스트리 MCP 가 붙는다 (아래 '알려진 제한'). 그래서 `mcp=[]` 로는 프로젝트의 MCP 가 빠지지 않고, `chat_mcp` 가 비어도 프로젝트에 unreal 이 있으면 채팅 턴에 붙는다.
- 거르는 자리는 `create` 의 MCP 한정 인자 `mcp_only` 다 (S-7 확정. §11.2). 런에 `RunState.mcp_only` 로 남고 `None` 이면 거르지 않는다. 준 이름만 남기고, 단계에 붙이는 목록(`_attached_mcp`)과 런을 만들기 전의 목록(`_run_mcp_names`)이 같은 값으로 거른다. 채팅 · 읽기 턴은 `chat_mcp` + `studio` 로 한정한다. 릴레이 단계가 적은 `mcp` 는 릴레이 파일의 것이라 거르지 않는다. 작업 런에는 주지 않는다 (S-8 확정. 프로젝트의 MCP — 에디터 등 — 는 작업의 일부라, 거르면 그 MCP 로 고치는 릴레이가 말없이 깨진다) — `employee.yaml` 의 `mcp` 에 없는 프로젝트 MCP 도 지금처럼 붙는다. 어느 쪽으로 정하든 서버 코드를 품은 프로젝트는 T3 의 규칙이 받는다 — 한정 인자는 릴레이 단계의 `mcp` 와 직원 자신의 `mcp` 를 거르지 못한다 (§4.3 T3).
- 채팅 턴 · 읽기 전용 턴: 릴레이는 `relays/employee-chat.yaml`(S-7), 칸은 `chat` 하나, MCP 는 `chat_mcp` + `studio` 다 (위의 한정 인자로 건다). 덮어쓰기는 없다. `start` 요청이 없으므로 값은 턴을 넣는 순간에 한 번만 본다. 6 은 같다 — 상한을 넘으면 낮춘다. 5 도 같다 — (가)는 studio 가 보고 (나)는 엔진의 검사에 맡긴다.
- 채팅 · 읽기 턴을 넣지 못할 때 (채팅 턴은 S-7, 읽기 턴은 S-9 확정 — 둘 다 `studio/chat.py`). `employee.yaml` 을 읽지 못하거나(§9.6) 5 의 (가)에 걸리거나 엔진이 `create` 를 거부하면 턴을 넣지 않는다. 거부할 `start` 가 없는 길이라 §4.5 의 왕복 한도 행과 같은 꼴로 한다 — 결재를 만들지 않고, `exchange_count` 도 올리지 않는다.
  - owner 의 글(§6.6)이 계기면: 글은 저장된 그대로 두고 REST 응답도 성공이다. 그 방에 `system` 글로 이유를 남긴다. `meta.event` 는 `run_error` 다 (§7.6. 종류를 새로 더하지 않는다). 런을 만들었는데 `done` 으로 끝나지 않은 턴도 같은 글을 남긴다(`meta.run_id` 가 그 런). 어느 쪽이든 `fed_message_id` 는 올리지 않아 다음 턴이 그 글을 다시 읽는다 (S-7). `done` 으로 끝났는데 답(`output`)이 비어 있고 그 런이 방에 쓴 글(`room_post`. `messages.run_id` 가 그 런)도 없으면 같은 `run_error` 글을 남긴다(`meta.run_id` 가 그 런). 이때는 `fed_message_id` 를 올린다 (§3.7) — 그 글은 이어 갈 대화에 이미 들어 있어, 올리지 않으면 다음 턴에 두 번 들어간다. 답이 비어도 그 런이 방에 글을 썼으면 그 글이 답이고 `run_error` 글은 남기지 않는다 (`ChatTurns.turn`). 단체방은 답이 빈 턴을 다르게 본다 (아래 '단체방의 턴').
  - 직원의 멘션(`room_post`, §8.4)이 계기면: (가)는 글을 만들기 전에 본다. 걸리면 글도 턴도 만들지 않고 `precondition_failed` 와 `failed[]`(`provider_unguarded`)를 돌려준다. 엔진의 거부는 글을 만든 뒤에 나므로 owner 의 글과 같이 `system` 글을 남기고, 도구 결과의 `queued[]` 에서 그 직원이 빠진다. `employee.yaml` 을 읽지 못한 경우(§9.6)도 엔진의 거부와 같이 글을 만든 뒤에 처리한다 (S-9 확정 — (가)의 코드가 아니라 `failed[]` 로 돌려줄 것이 없다). 담당을 멘션했는데 작업이 `working` 이 아니면 `system` 글 없이 `queued[]` 에서만 빠진다.
  - 인사의 판단 턴과 면담 이어받기 (T5): 둘 다 인사 1:1 방의 채팅 턴이라 값 · 릴레이 · 폴더 · 채용에 쓰는 값이 같다. 판단 턴은 서버의 `staffing.signal` 글만 새 글인 턴이고 조용하다(§8.4). 인사 직원이 없거나 그 1:1 방이 없으면 물을 곳이 없어 `Hooks.staffing_ask` 가 `False` 이고 기록은 되돌려진다. 인사는 있는데 턴을 넣지 못했으면(위 '넣지 못할 때') 글은 방에 남아 인사의 다음 턴이 읽으므로 물은 것으로 친다(`True`).
  - 단체방 · 파트방의 턴 (§6.6): 값 · 릴레이 · 폴더는 1:1 방의 채팅 턴과 같고 (`mode = chat`), 세션 제목만 방 이름이다. 넣지 못한 턴과 `done` 으로 끝나지 않은 턴은 owner 의 글이든 직원의 멘션이든 위와 같이 `run_error` 글을 남기고 `fed_message_id` 를 올리지 않는다. `done` 으로 끝났는데 답이 비어 있고 그 런이 방에 쓴 글도 없으면 넘어간 것이다 — 단체방 · 파트방에서만 `run_error` 글을 남기지 않고, `fed_message_id` 는 올린다. 그래서 단체방에서는 답이 빈 실패와 넘어감을 구분하지 못한다 (둘 다 글이 없다). 넘어가는 법은 턴에 넣는 새 글 끝의 `시스템:` 줄과 `prompts/employee-chat.md` 가 안내한다 — 실제 모델이 따르는지, 릴레이의 결과 검사가 빈 `output` 을 `done` 으로 받는지는 자동 테스트로 확인하지 못했다 (`VERIFICATION.md`). `exchange_count` 는 §4.6 의 '단체방의 왕복 한도'대로 센다.
- 채팅 · 읽기 턴의 릴레이와 작업 폴더 (채팅 턴은 S-7, 읽기 턴은 S-9 확정). 읽기 턴은 작업방에서 담당이 아닌 구성원에게만 만든다 — 담당의 그 방 대화는 작업 런이 가진다 (§3.7). owner 가 작업방에서 담당을 멘션해도 턴은 따로 생기지 않는다. 읽기 턴의 답(`output`)은 방에만 남는다. 담당의 작업 런에 닿게 하려면 그 구성원이 `room_post(mentions=[담당])` 을 불러야 하고, 턴에 넣는 새 글 끝의 `시스템:` 줄이 그렇게 안내한다 (실제 모델이 따르는지는 자동 테스트로 확인하지 못했다). `employee-chat.yaml` 의 단계는 provider 를 Claude Code 로 적고 `mcp` 는 적지 않는다. `hidden` 으로 둬 `auto` 의 후보에서 빠지게 한다 (`router.py` 47). `create` 는 `workdir` 나 `repo`, 아니면 세션의 작업 폴더가 있어야 한다 (`pipeline.py` 539–542). 작업방의 읽기 턴은 그 작업의 프로젝트, 그 밖의 방은 직원이 배치된 프로젝트의 `repo` 로 만든다 — 프로젝트의 지침이 함께 실리고 프로젝트의 MCP 는 한정 인자가 거른다. 프로젝트 밖의 폴더를 주면 MCP 는 빠지지만 지침도 빠진다. 미배치 직원과 인사는 `workdir` 를 그 직원 폴더로 준다 (§6.6 의 허용 폴더가 이 값을 쓴다).
  - 프로젝트 맥락이 없는 런의 MCP 와 알려진 제한 (확정 — 서버 3단계에 앞서 메웠다. `pipeline.py` `_attached_mcp` · `_env_mcp`. 자동 테스트는 `tests/test_studio_mcp.py` · `test_studio_chat.py` · `test_studio_work.py`). 맥락이 없는 때는 둘이다 — 저장소 설정의 `project_context` 가 꺼져 있을 때와, 그 폴더와 위 폴더들에 표식 파일(CLAUDE.md · `.claude/` · `.mcp.json`)도 사용자 MCP 도 없어 `projctx.discover` 가 `None` 을 돌려줄 때다. 미배치 직원의 폴더가 둘째 경우에 걸릴 수 있고, 걸리는지는 PC 마다 다르다. 그때에도 `studio` 를 고른 런 — 채팅 턴과 작업 런 — 에는 `studio` 가 붙는다. 붙는 길은 둘이다. 하나는 env 콜백이다 — studio 장부에 있는 런이면 `mcp_env` 콜백이 준 `studio` 를 맥락과 무관하게 단계에 붙인다 (`_env_mcp`, §8.2 '주입'). 다른 하나는 `_attached_mcp` 다 — 맥락이 없어도 `RunState.mcp` 에 `mcp_env` 에 등록된 이름(`studio`)이 있으면 그 런이 고른 레지스트리 MCP 를 붙이고 `mcp_only` 로 거르며, 붙은 것이 있으므로 ToolSearch 도 준다. 그래서 `RunState.mcp` 가 레지스트리 이름뿐이면 실제로 붙은 목록과 같다. 남는 제한은 셋이다. 첫째, 프로젝트 MCP 와 사용자 MCP(unreal 등)는 맥락 없이는 찾지 않아 붙지 않는다 — 런이 골랐으면 `mcp_missing` 이벤트가 남고 그 이름만큼 `RunState.mcp` 와 붙은 목록이 어긋난다. 둘째, 그 런에는 `project_context` 이벤트와 프롬프트의 MCP 안내 줄이 없고 프로젝트 지침 · 스킬도 실리지 않는다 — `project_context: false` 의 뜻은 그대로다. 셋째, `studio` 를 고르지 않은 런(대시보드 · CLI · 기존 캠페인)은 레지스트리 이름을 골라도 전처럼 아무것도 붙지 않는다. `studio` 를 골랐지만 장부에 없는 런(`relay run --mcp studio`)은 토큰이 없어 `studio` 는 빠지고, 함께 고른 레지스트리 MCP 와 ToolSearch 만 붙는다. 실서버 확인은 `VERIFICATION.md` 에 있다.
- `failed[]` 의 `unknown_stage` · `provider_unguarded` · `model_unavailable` · `effort_unsupported` · `model_over_cap` · `effort_over_cap` 은 S-8 확정이다 (`studio/work.py` 의 `CODES`. 걸음 4 · 5 · 6 의 순서). 문제 하나는 `{code, stage, message}` 다. `provider_unguarded` 는 §9.4 의 8 과 같은 코드다.

### 9.6 쓰는 길과 파트 값

| 길 | 계약 | 누가 |
|---|---|---|
| 채용 | `hire` 결재의 `approve`, `action = hire`. `draft.employee_yaml` 을 쓴다 (§5.5) | owner 의 결정 |
| 직접 수정 | `PUT /studio/employees/{id}/files/employee.yaml` (§6.4) | owner |
| 인사의 수정안 | `hire` 결재의 `approve`, `action = guide_amend`, `file = employee.yaml` (§5.5) | owner 의 결정 |
| 되돌리기 | `POST /studio/employees/{id}/revert` (§6.4) | owner |

- 넷 모두 §9.4 검사 → 파일 쓰기 → 직원 폴더 커밋(H-3) → `employee.updated`(`change = file`, §7.4) 순서다. 검사에 걸리면 쓰지 않는다. `revert` 는 되돌린 뒤의 `employee.yaml` 을 검사한다 — 그 버전이 지금은 없는 릴레이나 MCP 를 가리키면 `precondition_failed` 다. 채용과 인사의 수정안은 S-7 이 검사를 이었다 (`Inbox._check_yaml`). 직접 수정과 되돌리기는 그 경로를 만드는 H-3 이 같은 `employees.check` 를 부른다.
- 순서의 덧붙임 (H-3 확정). 위 순서 그대로인 것은 직접 수정과 되돌리기다. 결재의 두 길(채용 · 인사의 수정안)은 커밋이 맨 뒤다: 검사 → 파일 쓰기 → 결정 트랜잭션과 이벤트 → 커밋 (§5.5). 커밋을 결정 앞에 두면 트랜잭션이 실패해 파일을 되돌릴 때 이미 남은 커밋과 어긋나기 때문이다. 채용의 이벤트는 `change = created` 다.
- 직원 폴더의 커밋 (H-3 확정. `studio/employee_git.py`). 한 번의 쓰기가 폴더 전체의 커밋 하나다(`git add -A` → `git commit`). 그래서 그 폴더에 손으로 고쳐 둔 미커밋 변경도 다음 커밋에 함께 담긴다. 메시지는 길마다 정해져 있다: `hire: <이름> (<직원 id>)` · `guide_amend: <file> (<결재 id>)` · `edit: <file>`(직접 수정) · `revert: <sha 앞 7자>` · `adopt: <직원 id>`(시작 때의 첫 커밋) · `keep: before revert`(아래). 작성자는 `agent-katae <agent-katae@localhost>` 고정이다 — 전역 git 설정과 `GIT_AUTHOR_*` · `GIT_DIR` 같은 환경 변수를 따르지 않고, 위 폴더의 저장소로 올라가지 않는다 (§2.2). 푸시는 없다. 커밋이 실패하면(git 이 없다 · 거부 · 30초 초과) 쓴 파일은 그대로 두고 로그를 남긴다 — 직접 수정과 되돌리기의 응답은 `commit: null` 이고 이벤트는 그대로 나간다. 잃는 것은 중간 버전 하나다.
- 시작할 때의 첫 커밋 (H-3 확정). `server.py` 의 startup 함수 `commit_studio_folders` → `api.commit_folders()` 가 직원 행(퇴사 포함)의 폴더 중 `.git` 이 없는 것마다 저장소를 만들고 지금 내용 그대로 첫 커밋(`adopt:`)을 한다. 파일 내용은 바꾸지 않는다. 폴더를 만드는 startup 함수(시드 · 인사, §3.15) 뒤에 돈다. `.git` 이 이미 있는 폴더는 미커밋 변경이 있어도 건드리지 않는다. 폴더 하나가 실패하면 기록하고 다음으로 가며 서버는 뜬다.
- 전역 git 설정과 결재의 잠금 (H-3 마무리 확정). 직원 폴더의 git 호출은 모두 `-c core.hooksPath` · `-c core.attributesFile` 을 빈 장치(`os.devnull`)로, `-c core.autocrlf=false` 로 덮는다 (`employee_git.GLOBAL_OUT`). 전역 설정의 훅이 돌지 않고(`--no-verify` 가 건너뛰지 않는 `post-commit` · `prepare-commit-msg` 포함. 폴더의 `.git/hooks` 도 함께 꺼진다), 전역 속성 파일과 줄바꿈 변환이 커밋되는 바이트를 바꾸지 않는다 — 되돌리기는 커밋의 바이트를 그대로 쓴다. 전역 설정 자체는 끊지 않는다 (`safe.directory` 가 거기 있다). 덮지 않는 것: `init.templateDir` 로 들어오는 `.git/info/attributes`, `GIT_EXTERNAL_DIFF` 같은 환경 변수. 전에 줄바꿈이 바뀌어 커밋된 파일(작업 폴더는 CRLF, 커밋은 LF)이 있는 저장소는 다음 커밋에 그 파일이 한 번 변경으로 잡힌다. 결재의 `guide_amend` 승인은 base 비교부터 커밋까지 `employee_git.lock` 을 잡는다 (`Inbox._amend` · `_replace_file`) — 직접 수정 · 되돌리기와 같은 잠금이라, 같은 파일에 REST 쓰기와 승인이 겹치면 뒤에 온 쪽이 `precondition_failed` 다. `POLICY.md` 의 승인도 같은 함수라 같은 잠금을 잡는다.
- 직접 수정 (H-3 확정): 검사(`employee.yaml` 은 §9.4, `MEMORY.md` 는 §9.1 의 크기) → 지금 파일의 SHA-256 을 `base_sha256` 과 비교 → 쓰기 → 커밋 → 이벤트. 비교가 어긋나면 `precondition_failed` 이고 `failed[]` 는 `base_mismatch` 다 — 앱은 파일을 다시 읽고 고쳐 보낸다. 같은 파일을 한 base 에서 고친 두 요청은 먼저 온 것만 통과한다. 결재의 `guide_amend` 가 같은 조건에 걸렸을 때의 `failed[]` 는 빈 배열 그대로다 (§5.5).
- 되돌리기 (H-3 확정): 그 커밋에 있던 네 파일의 본문으로 폴더를 되돌리고 새 커밋을 만든다. 이력은 지우지 않으므로 되돌린 것을 다시 되돌릴 수 있다. 그 커밋에 없던 파일은 건드리지 않는다. 검사는 쓰기 전에 그 커밋의 `employee.yaml` 본문으로 한다 — 걸리면 네 파일 어느 것도 쓰지 않는다. 이력에 없는 커밋(sha 가 아닌 이름 포함)은 `precondition_failed` 이고 `failed[]` 는 `commit_unknown` 이다. 폴더가 이미 그 커밋과 같으면 쓰지 않고 지금의 마지막 커밋을 돌려주며 이벤트도 없다. 쓰기 전에 폴더에 미커밋 변경이 있으면 그것부터 `keep: before revert` 로 커밋해 되돌리기가 손으로 고친 내용을 지우지 않게 한다.
- 알려진 제한 (H-3). 첫째, 미배치 직원과 인사의 채팅 턴은 작업 폴더가 직원 폴더다 (§9.5). 그 폴더가 저장소가 된 뒤로 그 런의 변경 기록(`journal.detect_vcs`)은 직원 폴더의 git 을 본다. 런 도중 서버가 그 폴더에 커밋하면 기록이 보는 기준이 새 커밋이 된다. 직원은 그 폴더에 쓰지 못하므로 코드는 그대로 뒀다. 둘째, `git checkout <커밋> -- employee.yaml` · `git restore` · `git stash` 처럼 git 이 대신 쓰는 명령은 H-3 마무리에서 가드가 막는다 — 보호 폴더 안의 저장소에서는 읽는 하위 명령만 통과한다 (§10.3 의 6). 직원 폴더가 작업 폴더인 실제 채팅 턴에서 거부되는지는 사람이 확인한다. 셋째, 휴직 · 퇴사한 직원의 파일도 고칠 수 있다 — `PUT files` · `revert` 는 직원의 상태를 보지 않는다 (H-3 확정).
- 직원은 쓰지 못한다. studio MCP 에 이 파일을 쓰는 도구가 없고(인사의 `employee_draft` 는 초안만 만든다, §8.4), 셸과 편집 도구로 직원 폴더에 쓰는 것은 가드가 막는다 (§10.3 의 `protected_write`). 그래서 직원이 자기 상한을 올릴 수 없다.
- **파트는 채용 뒤 바뀌지 않는다.** 파트를 바꾸는 REST 경로가 없다 (§6.4). `department` 는 `employees.department_id` 의 사본이고, 다르게 쓰려는 요청은 `department_mismatch` 로 거부한다. 읽을 때 둘이 다르면(파일을 손으로 고친 경우) DB 를 따른다 (§2.3). 파트를 옮기려면 퇴사시키고 새로 채용한다.
- 읽지 못할 때: 파일이 없거나 §9.4 의 1–3 에 걸리면 그 직원의 턴과 작업 런을 만들지 않는다. 작업이면 §4.3 T3 의 실패 처리를 따르고, 채팅이면 방에 시스템 글을 남긴다 (S-7 확정).

---

## 10. 권한 · 가드

근거: 전제 4 · 5, S-10, CAMPAIGN.md §7(526–540행). §6.4 의 가드 소절이 미룬 것을 여기서 받는다. 서버 쪽 통로(`POST /studio/guard/check`)와 분기는 §6.4, 결재는 §5, 코드 접점은 §11.4 다.

### 10.1 `studio-full` 프로파일

기존 코드에 이름 붙은 권한 프로파일은 없다. 권한은 릴레이 단계의 `tools` · `allowed_tools` · `permission_mode`(`pipeline.py` 96–99)와 `Config.extra_allowed_tools`(`config.py` 44)로 정해진다. `studio-full` 은 둘을 묶은 이름이다: studio 런의 모든 단계에 **더하는 사전 승인 묶음**과 **가드 훅**. studio 런은 작업 런과 채팅 · 읽기 턴의 런이다 (§8.2).

| 런의 `mode` (§7.4) | 더하는 사전 승인 | `tools` · `permission_mode` |
|---|---|---|
| `work` (작업 런) | `Bash` · `Edit` · `Write` · `MultiEdit` · `NotebookEdit` · `Skill` · `mcp__studio` · 붙은 MCP 서버마다 `mcp__<이름>` | 릴레이 파일의 값 그대로. 단 스킬을 끄는 플래그(`--safe-mode` · `--disable-slash-commands`)는 없다 (`isolate` 해제) |
| `read` · `chat` | `mcp__studio` · `chat_mcp` 의 서버마다 `mcp__<이름>` | 릴레이 파일의 값 그대로 |

- 사전 승인은 도구를 늘리지 않는다. 러너는 쓸 수 있는 도구를 `--tools` 로, 묻지 않고 실행할 것을 `--allowedTools` 로 따로 넘긴다 (`runners.py` 604–610). `tools: [Read, Grep, Glob]` 인 단계는 `Bash` 가 사전 승인돼도 셸을 쓰지 못한다. S-10 의 "편집 · 셸 · MCP 사전 승인"을 이렇게 읽었다 — 릴레이가 준 도구 안에서는 묻지 않는다.
- 단, 릴레이 단계가 준 `Bash` 는 작업 런에서 빠지지 않는다 (owner 결정, 2026-10-14). 엔진은 검증 명령도 Bash 허용 항목도 없는 저장소에서 단계의 `Bash` 를 `Grep` · `Glob` 으로 바꾸는데(`pipeline._stage_tools`), 이 때문에 MNYS 같은 저장소의 작업 런에 셸이 없었다(실제 런에서 확인 — 가드까지 가지도 못했다). 가드가 걸린 런의 프로파일이 `Bash` 를 승인하면(`work`) 엔진이 그 `Bash` 를 되돌려 놓고, 명령마다 훅이 판정한다. 채팅 · 읽기 턴(프로파일에 `Bash` 없음)과 릴레이가 `Bash` 를 주지 않은 단계는 그대로다.
- `mcp__<이름>` 은 러너가 이미 넣는다(606). studio 가 더할 것은 `Bash` 전체와 편집 도구 넷이다. 이름만 적은 `Bash` 가 모든 셸 명령을 승인하는지는 확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "이름만 적은 `Bash` 사전 승인이 모든 셸 명령을 승인하는지") — 저장소의 선례는 `Bash(uv run:*)` 같은 접두사 형식뿐이다.
- 게임 프로젝트의 런(`projctx.game_root(run.workdir)` 이 `*.uproject` 나 `ProjectSettings/ProjectVersion.txt` 가 있는 폴더를 찾는 런. `CLAUDE.md` 등 마커가 없어도 된다)은 studio 런이든 릴레이 런이든 `Skill` 이 도구 목록과 사전 승인에 붙고 `isolate` 가 풀려 스킬을 끄는 플래그가 없다 — 사용자 폴더의 하네스 스킬(`~/.claude/skills`)을 쓴다. 단계가 `Bash` 를 가지면 `_stage_tools` 가 `Grep` · `Glob` 으로 바꾸지 않고, 인덱스 조회 명령이 `Bash(<명령>)` · `Bash(<명령>:*)` 와 `Bash(cd:*)` 로 사전 승인된다. 명령은 하네스(`~/.claude/skills/game-onboard` 정션 기준, `harness_root.resolve_root("")`)의 `skills/*/SKILL.md` 에 백틱으로 적힌 `python Tools/x.py` 형태에서 뽑고(`projctx.harness_commands`), `projctx.INDEX_COMMANDS`(`python Tools/gq.py` · `cindex.py` · `ue_q.py` · `ue_q`)는 문서를 못 찾거나 정규식이 못 잡는 명령(`ue_q` 단독 실행 등)을 위한 대체 추정값이다. 설정 `studio_harness_root` 는 이 경로에 쓰이지 않는다(엔진에 Config 가 없다) — 정션이 없으면 추정값만 쓰인다. 게임 프로젝트가 아닌 저장소의 런은 그대로다.
- 작업 런(`work`)은 프로젝트와 상관없이 `guard_stage` 가 `skills: true` 를 돌려주어 엔진이 `Skill` 을 붙이고 `isolate` 를 푼다. 채팅 · 읽기 턴은 그대로다. 가드의 판정 규칙(`check` · `profile` · `studio_guard`)은 고치지 않았다 — 훅이 명령마다 판정하는 것은 같다.
- `permission_mode` 를 `bypassPermissions` 로 올리지 않는다. 올리면 모르는 도구까지 열려 훅 한 겹에만 기대게 된다. 사전 승인에 없는 도구는 헤드리스 런에서 거부되고, 그것이 둘째 겹이다.
- 들어가는 자리는 `StageCall` 의 `allowed_tools` 를 조립하는 줄이다 (`pipeline.py` 1657–1659, §11.4).
- 가드 훅은 세 `mode` 모두에 건다. 읽기 전용 턴도 `Read` 로 비밀을 읽을 수 있다.
- 모든 직원이 같은 프로파일을 쓴다 (전제 4). 파트와 담당은 판정에 들지 않는다 — 담당 밖 변경은 막지 않고 검수 패키지의 `out_of_scope[]` 로 보인다 (§5.4).

### 10.2 훅의 입력과 정규화

`relay_agent/studio_guard.py` 는 Claude Code 가 도구를 실행하기 직전에 부르는 프로세스다. DB 를 열지 않는다 (§3.1).

| 입력 | 내용 |
|---|---|
| stdin | 도구 호출 JSON. `tool_name` · `tool_input` · `cwd` 를 읽는다. 기존 훅이 읽는 것은 `tool_input.file_path` · `notebook_path` 뿐이라(`backup_hook.py` 47–53) `tool_name` · `cwd` 필드가 오는지는 확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "Claude Code 가 훅에 주는 입력이 판정기가 읽는 것과 같은지"). S-10 확정: `tool_name` 이 없으면 `guard_error` 로 거부하고, `cwd` 가 없으면 훅 프로세스의 현재 폴더를 쓴다 |
| argv | `guard_ctx.json` 의 경로 하나 |
| 환경 변수 | `STUDIO_URL` · `STUDIO_ACTOR_TOKEN` (§11.4). 토큰은 서버에 물을 때 그대로 싣는다. `claims` 의 `run` · `emp` · `room` · `task` · `mode`(§8.2)는 감사 로그에만 쓴다 — 훅은 서버 비밀이 없어 서명을 검증하지 못하므로 판정에 쓰지 않는다 |
| `runs/<run_id>/guard_ctx.json` | 서버가 런을 만들 때 쓴다. 비밀은 넣지 않는다 |

`guard_ctx.json` 의 필드:

| 필드 | 내용 | 쓰는 규칙 |
|---|---|---|
| `v` · `run_id` | `1`, 이 런의 id | 감사 로그 |
| `server_root` | 서버 저장소의 루트 (`config.py` 의 `ROOT`) | `protected_write` · `secret_access` |
| `runs_dir` | `Config.runs_dir` | 감사 로그의 자리 |
| `server_url` · `server_pid` | 서버 주소(`Config.server_url`, `config.py` 23)와 서버 프로세스 id | `server_control` |
| `protected[]` | 쓰기 · 삭제 · 이동을 막는 절대 경로 (§10.8) | `protected_write` |
| `secrets[]` | 읽기도 막는 절대 경로 (§10.3 의 3). `Config.studio_fcm_key` 가 가리키는 키 파일의 경로가 끝에 더해진다 (§5.9) | `secret_access` |
| `delete_roots[]` | 삭제 · 이동을 묻지 않는 루트 (§10.4 의 2) | `outside_delete` |
| `known_hosts[]` | 올리기를 묻지 않는 호스트 (§10.4 의 5) | `net_transfer` |

**`tool` 과 `command`.** `tool` 은 Claude Code 가 준 `tool_name` 그대로다. `command` 는 판정한 호출의 원문이다 — 셸이면 `tool_input.command` 전체, 그 밖에는 `tool_input` 을 한 줄 JSON 으로 적은 것. 두 값이 `POST /studio/guard/check` 의 요청과 `risky_command` 의 payload 에 들어간다 (§5.3, §6.4).

| `tool` | 보는 것 |
|---|---|
| `Bash` (셸 도구) | `tool_input.command` 를 아래 방법으로 풀어 조각마다 본다 |
| `Edit` · `Write` · `MultiEdit` · `NotebookEdit` | 쓰기 대상 경로 `file_path` · `notebook_path` |
| `Read` · `Grep` · `Glob` | 읽기 대상 경로 |
| 주소를 받는 도구 (`WebFetch` 등) | 주소의 호스트와 포트 |
| `mcp__studio__*` | 보지 않고 통과시킨다. 서버가 도구별 권한을 본다 (§8.5) |
| 그 밖의 `mcp__*` 와 모르는 도구 | 인자의 문자열 값에서 경로로 읽히는 것을 모아 경로 규칙(`secret_access` · `protected_write`)만 본다 |

- 셸 도구의 이름이 `Bash` 하나인지(Windows 에 PowerShell 도구가 따로 있는지)와 읽기 도구의 인자 이름은 확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "Claude Code 가 훅에 주는 입력이 판정기가 읽는 것과 같은지"). 판정기가 받는 셸 도구의 이름은 `studio_guard.py` 의 `SHELL_TOOLS` 에 있다 — `Bash` · `PowerShell` (S-10 확정).
- S-10 확정 (판정기가 읽는 것. 설치된 Claude Code 에서의 실제 이름은 사람이 한 번 확인한다): 셸 도구는 `Bash`(bash 문법)와 `PowerShell`(PowerShell 문법) 둘이고 명령은 둘 다 `tool_input.command` 다. `Read` 는 `file_path`, `Grep` 은 `path` · `glob`, `Glob` 은 `path` · `pattern` 을 본다 — `path` 가 없으면 `cwd` 이고 둘은 재귀 읽기다. 최상위 `url` · `uri` 가 http(s) 인 도구가 주소를 받는 도구다. 그 밖의 도구는 값 전체가 경로 모양(드라이브 · `\\` · `/` · `~` · `./` · `../` 로 시작)인 문자열만 모은다.

**셸 명령 풀기.** S-10 의 완료 기준이 요구하는 우회 변형(경로 표기 바꾸기, 체인 명령, PowerShell · cmd · bash)을 여기서 받는다.

1. 조각으로 나눈다: `;` · `&&` · `||` · `|` · `&` · 줄바꿈, 그리고 `$( )` · 백틱 · `<( )` 의 안. 따옴표 안은 나누지 않는다.
2. 안쪽 셸을 편다: `cmd /c` · `cmd /k`, `powershell` · `pwsh` 의 `-Command` · `-c`, `-EncodedCommand` · `-enc`(base64 로 적은 UTF-16LE 를 푼다), `bash` · `sh` 의 `-c`, `wsl`, `eval`, `Invoke-Expression` · `iex`, `Start-Process` · `start` 의 대상, `xargs` · `env` · `sudo` · `call` 뒤의 명령. 편 본문은 1 부터 다시 한다. 깊이는 5 까지다 (S-10 확정, `MAX_DEPTH`). S-10 이 더한 것: `uv run` · `nohup` · `time` · `command` · `exec` · `winpty` 뒤의 명령과 `find` 의 `-exec` 도 편다. 명령 없이 뜬 셸은 heredoc · here-string · 바로 앞 `echo` 의 파이프를 본문으로 편다 — 그 밖의 파이프를 읽는 셸(`… | sh`)과 본문이 보이지 않는 `iex` 는 `unparsed` 다. 스크립트 파일(`bash x.sh` · `-File x.ps1`)은 열어 보지 않는다 (§10.8).
3. 명령 이름을 맞춘다: 경로와 확장자를 떼고 소문자로 바꾼다 (`C:\Windows\System32\shutdown.exe` → `shutdown`). PowerShell 별칭을 원래 이름으로 바꾼다 (`rm` · `del` · `rd` · `ri` · `erase` → `Remove-Item`). 캐럿(`^`) · 백틱 이스케이프와 따옴표로 쪼갠 이름(`sh"ut"down`)을 붙인다.
4. 경로를 맞춘다: 따옴표를 떼고, 환경 변수(`%USERPROFILE%` · `$env:USERPROFILE` · `$HOME` · `~`)를 펴고, `/c/…` · `/mnt/c/…` 를 `C:\…` 로 바꾸고, 슬래시를 한쪽으로 맞추고, `.` · `..` 를 정리하고, 상대 경로는 `cwd` 에 붙이고, `\\?\` 접두사를 떼고, 있는 경로는 `resolve` 해 8.3 짧은 이름 · 심볼릭 링크 · 정션을 따라간다. 비교는 대소문자를 가리지 않는다. S-10 확정 (`resolve`): `os.path.realpath` 로 따라가고, 결과에 붙은 긴 경로 접두사는 `\\?\C:\…` → `C:\…`, `\\?\UNC\서버\공유\…` → `\\서버\공유\…` 로 되돌린다. 아직 없는 파일은 있는 앞부분만 따라간다. 정션 · 심볼릭 링크 · 8.3 이름은 실제 디스크로 시험한다 (`tests/test_studio_guard.py` 의 `…_on_a_real_disk` 셋. 링크나 짧은 이름을 만들 수 없는 PC 에서는 그 시험을 건너뛴다).
  - S-10 확정 (폴더 이동을 따라간다): 한 조각이 폴더를 옮기면(`cd` · `chdir` · `pushd` · `Set-Location` · `sl`, cmd 의 `cd /d`, `git -C` · `env -C`, `Start-Process -WorkingDirectory`) 다음 조각의 상대 경로는 옮긴 폴더에 붙인다. `cmd /c "cd /d C:\ && rd /s /q Windows"` 의 `Windows` 는 `C:\Windows` 다. 옮긴 대상이 변수이거나 `cd -` · `popd`(스택은 추적하지 않는다)면 그 뒤의 상대 경로는 `unparsed` 다 — 절대 경로는 그대로 본다. `git -C` 는 다음 조각의 폴더를 옮기지 않는다. 그 git 명령 자신의 판정에만 든다 (H-3 마무리 확정): `protected_write` 가 `-C` · `--work-tree` · `--git-dir` 로 git 이 일하는 폴더를 읽는다 (§10.3 의 6). `git_risky` 는 경로를 보지 않는다.
  - S-10 확정 (bash 방언의 추가 표기): `/tmp` 는 `%TEMP%` 로, `/cygdrive/c/…` · `/proc/cygdrive/c/…` 는 `C:\…` 로 편다. 방언과 무관하게 이 PC 를 가리키는 관리 공유(`\\localhost\c$\…` · `\\127.0.0.1\c$\…` · `\\<이 PC 이름>\c$\…`)는 `C:\…` 로 본다 — 다른 호스트의 공유는 그대로 둔다. bash 에서 이 관리 공유는 따옴표로 감싸야 역슬래시가 남는다(`$`·`\` 가 안 그러면 변수·이스케이프로 먹힌다). `c$` 의 `$` 처럼 변수 이름을 이루지 못하는 `$` 는 글자 그대로다.
5. 풀지 못하면 `unparsed` 다 — 명령 이름 자리가 변수이거나, 안쪽 셸의 본문을 펴지 못했거나(변수에 든 본문, 깊이 초과, 해독 실패), 경로를 보는 규칙의 명령인데 대상 경로에 펴지 못한 변수가 있을 때다. 그 밖의 명령에서 인자에만 변수가 있는 것(`echo $f`)은 해당하지 않는다. `unparsed` 는 거부한다 (§10.5). §7 에 없는 판정이다. `[§12]` owner 확정 (§12.6 의 8): 해석하지 못한 명령은 거부한다. 통과시키면 변수 한 번으로 모든 규칙을 지나가고, 거부하면 `rm "$f"` 같은 흔한 줄이 막혀 직원이 경로를 그대로 적어 다시 실행해야 한다.

### 10.3 차단 7항목

CAMPAIGN.md §7 의 차단 글머리 다섯 줄을 `rule_id` 일곱으로 나눴다. 첫 줄(529행)과 넷째 줄(532행)은 앞쪽이 명령 이름을, 뒤쪽이 대상 경로를 봐서 판정 입력이 다르다. 걸리면 서버에 묻지 않고 그 자리에서 거부한다.

| # | `rule_id` | §7 의 줄 | 보는 것 | 걸리는 때 |
|---|---|---|---|---|
| 1 | `disk_format` | 529 앞 | 명령 이름과 인자 | 포맷 · 파티션 명령이다: `format` · `diskpart` · `Format-Volume` · `Clear-Disk` · `Initialize-Disk` · `New-Partition` · `Remove-Partition` · `Resize-Partition` · `Set-Partition` · `mkfs*` · `fdisk` · `parted`, 그리고 출력이 원시 디스크(`\\.\PhysicalDrive*` · `/dev/sd*`)인 `dd` |
| 2 | `system_delete` | 529 뒤 | 삭제 명령의 재귀 표시와 대상 경로 | 재귀 삭제(`rm -r` · `Remove-Item -Recurse` · `rd /s` · `rmdir /s` · `del /s` · `robocopy` 의 `/MIR` · `/PURGE`)의 대상이 (가) 드라이브 루트나 사용자 프로필 루트 자신 또는 그 바로 아래 전체(`C:\*`)이거나 (나) `C:\Windows` · `Program Files` · `Program Files (x86)` 자신 또는 그 아래 어디든 |
| 3 | `secret_access` | 530 | 어느 도구든 인자에서 읽힌 경로 | 경로가 `secrets[]` 의 아래다. 읽기 · 쓰기 · 복사 · 전송을 가리지 않는다. 재귀로 읽는 것(`Grep` · `Glob` · `grep -r` · `findstr /s` · `Get-ChildItem -Recurse`)은 대상이 비밀 폴더의 상위(사용자 프로필 루트 · `C:\Users` · 드라이브 루트 · `AppData`)여도 걸린다 |
| 4 | `security_config` | 531 | 명령 이름과 인자, 레지스트리 경로 | Defender(`Set-MpPreference` · `Add-MpPreference`, `WinDefend` 서비스를 멈추거나 바꾸는 `sc` · `Stop-Service` · `Set-Service`), 방화벽(`netsh advfirewall` · `netsh firewall` · `*-NetFirewallRule` · `Set-NetFirewallProfile`), 계정 · 비밀번호(`net user` · `net localgroup` · `*-LocalUser` · `*-LocalGroupMember` · `wmic useraccount` · `passwd`), `HKLM` 에 쓰는 것(`reg` 의 `add` · `delete` · `import` · `restore` · `load`, `HKLM:\` 를 대상으로 한 `Set-ItemProperty` · `New-Item` · `Remove-Item`, `regedit /s`). UAC 는 `HKLM` 쓰기에 든다. `reg query` 같은 읽기는 걸리지 않는다 — M-1 이 엔진 목록을 레지스트리에서 읽는다 |
| 5 | `server_control` | 532 앞 | 프로세스를 끝내는 명령의 대상, 요청이 가는 주소, `relay` 의 하위 명령 | (가) `taskkill` · `Stop-Process` · `kill` · `pkill` · `wmic process … terminate` 의 대상이 `server_pid` 이거나, 이름으로 끝내는데 서버 프로세스의 이미지 이름과 같다. (나) 주소가 `server_url` 의 호스트와 포트인 요청 — `curl` · `wget` · `Invoke-WebRequest` · `Invoke-RestMethod` 와 그 별칭, 주소를 받는 도구. 루프백 표기(`127.0.0.1` · `localhost` · `[::1]` · `0.0.0.0` · 이 PC 의 이름과 주소)는 모두 같은 호스트로 본다. (다) 서버를 띄우거나 설정을 바꾸는 `relay` 하위 명령(S-10 확정: `serve` · `remote` · `autostart`. `uv run relay …` · `python -m relay_agent[.cli] …` · `uvicorn relay_agent…` 로 적어도 같다) |
| 6 | `protected_write` | 532 뒤 | 쓰기 · 삭제 · 이동의 대상 경로 | 경로가 `protected[]` 의 아래다 (§10.8). 편집 도구의 대상, 셸의 리다이렉션(`>` · `>>` · `tee` · `Out-File` · `Set-Content` · `Add-Content`), 복사 · 이동의 목적지, 삭제 · 이동의 대상, 제자리 편집(`sed -i`)을 본다. git 이 대신 쓰는 것도 본다 (아래 '6 의 git') |
| 7 | `payment_account` | 533 | 명령 이름과 하위 명령, 요청이 가는 호스트 | 결제 · 송금 API 호스트로 가는 요청, 서비스 CLI 의 계정 생성 · 결제 하위 명령. 호스트와 명령의 목록은 `studio_guard.py` 의 `PAY_HOSTS` · `PAY_CLI` 다 (S-10 확정, 아래) |

- 3 의 `secrets[]`: `%USERPROFILE%\.ssh`, 브라우저 프로필(Chrome · Edge · Firefox 의 사용자 데이터 폴더), Claude 자격 증명, `%USERPROFILE%\.katae\`, 서버 루트의 `relay.config.local.yaml`. 여기까지가 §7 이다. S-10 확정 (`studio_guard.default_secrets` 가 이 목록을 만들고 서버가 `guard_ctx.json` 에 적는다. 이 PC 의 실제 위치와 맞는지는 사람이 확인한다): 브라우저 프로필은 `%LOCALAPPDATA%\Google\Chrome\User Data` · `%LOCALAPPDATA%\Microsoft\Edge\User Data` · `%APPDATA%\Mozilla\Firefox\Profiles`, Claude 자격 증명은 `%USERPROFILE%\.claude\.credentials.json` · `%USERPROFILE%\.claude.json` 이다. 하나를 더했다 — 러너가 단계마다 쓰는 임시 폴더의 `mcp.json`(`%TEMP%\katae-*\`). 다른 런의 행위자 토큰이 들어 있다 (§8.2). `[§12]`
- 5 의 (나)는 §7 의 "릴레이 서버 종료"보다 넓다. `/admin/restart` 만이 아니라 서버로 가는 요청을 모두 막는다. `/studio/*` 는 루프백에도 토큰을 요구하지만(§6.2), `/studio` 가 아닌 기존 경로는 루프백을 그대로 믿고, 공용 토큰을 얻은 셸은 결재를 스스로 결정하거나 직원 파일을 고칠 수 있다 — 그 요청을 여기서 한 번 더 막는다. 직원은 studio MCP 로만 서버에 말한다 (전제 2). 포트까지 같아야 걸리므로 직원이 다른 포트에 띄운 시험용 서버는 걸리지 않는다. `[§12]`
- 6 의 `protected[]` 에 `runs/studio/` 전체와 서버 루트의 `relay_agent/` 전체를 넣은 것도 §7 에 없다 (§10.8). `[§12]`
- 7 은 목록에 있는 것만 잡는다. 브라우저를 조작하는 도구나 스크립트 안에서 하는 결제는 보지 못한다 (§10.8).
- 표의 명령은 대표 예다. S-10 이 항목마다 "명령 예시 → 기대 판정" 테스트로 목록을 고정한다.
- S-10 확정 (`tests/test_studio_guard.py` 의 표가 고정한다):
  - 3 의 재귀 읽기가 "상위여도 걸리는" 것은 폴더인 비밀뿐이다. 파일인 비밀(`relay.config.local.yaml` · `.credentials.json` · `.claude.json` · `mcp.json`)은 그 파일을 직접 가리킬 때만 걸린다 — 아니면 서버 저장소 루트의 `Grep` · `Glob` 이 모두 막힌다. 경로는 어느 조각의 어느 인자에 있든 본다(옵션의 `=` · `:` 뒤 값, `@파일`, 리다이렉션 대상 포함).
  - 5 의 (가): 이름으로 끝낼 때의 서버 이미지 이름은 `python` · `pythonw` · `python3` · `py` · `uv` · `relay` 다(`guard_ctx.json` 에 `server_images[]` 가 있으면 그것). 대상을 알 수 없는 종료(`Get-Process … | Stop-Process`, 대상 없는 `taskkill`)도 거부한다. (나): 주소는 명령 이름이 아니라 모든 인자에서 찾는다 — `python -c "…urlopen('http://localhost:<포트>/…')"` 도 걸린다.
  - 6: 삭제 · 이동은 대상이 `protected[]` 의 상위여도 걸린다 (`rm -rf <서버 루트>/runs`). 쓰기 대상에는 내려받기·풀기의 출력도 든다 — `curl -o` · `--output`, `wget -O` · `--output-document`, `-OutFile`, `Start-BitsTransfer -Destination`, 풀기의 `tar -x … -C <폴더>` · `unzip -d <폴더>` · `Expand-Archive -DestinationPath <폴더>` · `7z x -o<폴더>`. `aria2c` 의 `-d` · `--dir` 와 `-o` · `--out` 도 본다 (S-10 확정) — 둘을 이어 한 경로로 읽고, `-d` 만 있으면 이름이 URL 에서 오므로 그 폴더 바로 아래 전체를 대상으로 본다(서버 루트로 받는 내려받기는 이름과 무관하게 막힌다). 인터프리터 한 줄 본문(`python -c` · `node -e` 등)에 따옴표로 적힌 경로도 쓰기 대상에 든다 (§10.8 의 1). 못 보는 것(§10.8): 스크립트 파일 안에서 하는 쓰기, 출력 옵션 없이 `cwd` 에 받는 내려받기.
  - 6 의 git (H-3 마무리 확정. `studio_guard._git_protected`): git 이 대신 쓰는 것도 쓰기로 본다. git 이 일하는 폴더는 그 명령의 `cwd` 에 `-C` 를 차례로 붙인 것이고, `--work-tree` · `--git-dir` 값(`=` 꼴 포함)도 함께 본다. (가) 그중 하나가 `protected[]` 의 아래면 읽는 하위 명령만 통과한다 — `status` · `log` · `show` · `diff` · `rev-parse` · `ls-files` · `cat-file` · `blame` · `grep` 등(`GIT_READ`)과, 인자가 없거나 목록 플래그뿐인 `branch` · `tag` · `remote`. `-c` · `--config-env` · `--exec-path`, `--output`, `grep -O` 가 붙으면 읽기로 보지 않는다. 금지 목록이 아니라 허용 목록이라 `checkout` · `restore` · `reset` · `stash` · `commit` · `update-ref` 와 별칭(`-c alias.co=checkout co`)이 한 규칙으로 막힌다. 미배치 직원과 인사의 채팅 턴(작업 폴더가 직원 폴더, §9.5)에서 이력을 읽는 것은 되고 옛 `employee.yaml` 을 되살리는 것은 안 된다. (나) 그 밖의 폴더에서는 작업 트리를 경로로 덮어쓰는 하위 명령(`GIT_TREE`: `checkout` · `restore` · `reset` · `rm` · `mv` · `clean` · `stash` · `checkout-index` · `read-tree` · `worktree` · `clone` · `init` · `bundle` · `sparse-checkout` · `submodule`)과 내장 목록(`GIT_PLAIN`)에 없는 이름(별칭)의 인자를 일하는 폴더 기준 경로로 읽어, `protected[]` 의 아래나 상위면 막는다 — `cd <서버 루트> && git checkout HEAD~3 -- relay_agent/studio_guard.py`, 같은 줄의 `-- .`. 저장소 꼭대기 기준 pathspec(`:/x` · `:(top)x`)은 위 폴더마다 붙여 본다 (꼭대기를 디스크에서 찾지 않는다). (다) `--output`(어느 하위 명령이든), `archive` · `format-patch` 의 `-o`, `config` 의 `-f` · `--file` 이 가리키는 파일은 쓰기 대상이다. (라) `unparsed`: 일하는 폴더를 알 수 없는데(`-C $d`, 변수로 옮긴 `cd` 뒤) 읽기가 아닐 때, 보호 폴더 안의 읽기에 변수 인자가 있을 때, 일하는 폴더가 `protected[]` 의 상위인데 (나)의 인자가 변수일 때, (다)의 값이 변수일 때. 프로젝트 폴더의 `git checkout` · `restore` · `stash` · `commit` 은 그대로 통과한다. 남는 것은 §10.8 에 있다.
  - 7 의 호스트: `stripe.com` · `paypal.com` · `tosspayments.com` · `iamport.kr` · `portone.io` · `squareup.com` · `braintreegateway.com` · `adyen.com` · `wise.com` · `transferwise.com` · `kakaopay.com` · `coinbase.com` · `binance.com` · `upbit.com` · `bithumb.com` 과 그 하위 도메인. CLI: `stripe` 의 `charges` · `payment_intents` · `payouts` · `transfers` · `refunds` · `subscriptions` · `invoices` · `checkout` · `accounts` · `post` · `trigger`, `paypal`, `aws organizations create-account`, `aws iam create-user`, `gcloud billing`, `az account create`, `az ad user create`, `npm adduser`, `heroku signup`, `firebase projects:create`.

### 10.4 물어보고 진행 5항목

| # | `rule_id` | §7 의 줄 | 보는 것 | 걸리는 때 |
|---|---|---|---|---|
| 1 | `git_risky` | 536 | `git` 의 하위 명령과 인자. `-C <경로>` · `-c <설정>` 같은 전역 옵션은 건너뛴다 | `push` 전부(원격 브랜치 · 태그를 지우는 `--delete` · `:<이름>` 포함), `reset --hard`, `-f` 가 있고 `-n` 이 없는 `clean` |
| 2 | `outside_delete` | 537 | 삭제 · 이동 명령의 대상 경로. 이동은 원본과 덮어쓰이는 목적지 | 대상이 `delete_roots[]` 어느 것의 아래도 아니다 |
| 3 | `install_env` | 538 | 명령 이름, 하위 명령, 전역 표시 | 설치 · 제거: `winget` · `choco` · `scoop` 의 `install` · `uninstall` · `upgrade`, `msiexec` 의 `/i` · `/x`, `Add-AppxPackage` · `Remove-AppxPackage`, 전역 도구 설치(`npm install -g` · `pip install --user` · `pipx install` · `uv tool install` · `cargo install` · `dotnet tool install -g`). 전역 환경: `setx`, 범위가 `User` · `Machine` 인 `[Environment]::SetEnvironmentVariable`, `HKCU\Environment` 에 쓰는 것 |
| 4 | `power_state` | 539 | 명령 이름과 인자 | `shutdown`(취소인 `/a` 제외) · `Stop-Computer` · `Restart-Computer` · `rundll32 powrprof.dll,SetSuspendState` · `psshutdown` · `reboot` · `poweroff`, `systemctl` 의 `poweroff` · `reboot` · `suspend` · `hibernate` |
| 5 | `net_transfer` | 540 | 전송 명령의 방향, 대상 호스트, 크기 | (가) 내려받기(`curl` · `wget` · `Invoke-WebRequest` · `Start-BitsTransfer` · `bitsadmin` · `aria2c`)의 크기가 1GB(1024³바이트)를 넘는다. (나) 올리기(`curl` 의 `-T` · `-F` · `-d @파일` · `--data-binary @파일`, 원격이 목적지인 `scp` · `sftp` · `rsync` · `rclone` · `aws s3 cp`, `-InFile` 을 가진 `Invoke-WebRequest` · `Invoke-RestMethod`)의 호스트가 `known_hosts[]` 에 없다 |

- 1: CAMPAIGN.md 는 `git clean -fdx` 를 적는다. `-fd` · `-fx` · `--force` 같은 변형도 추적하지 않는 파일을 지우므로 같이 묻는다 (S-10 확정: `-f` 가 있고 `-n` · `--dry-run` 이 없는 `clean`). 통과(T7)의 커밋은 서버가 하므로 훅을 지나지 않는다. 보호 폴더 안의 저장소에서 도는 git 과 보호 경로를 덮어쓰는 git 은 차단의 `protected_write` 가 먼저 받는다 (§10.3 의 '6 의 git') — 직원 폴더의 `git reset --hard` 는 묻지 않고 거부한다.
- 2 의 `delete_roots[]`: 등록된 프로젝트 폴더 전부(`projects.path`, 보관된 것 제외), 이 런의 작업 폴더(`copy` 면 복사본), 임시 폴더(`%TEMP%`). 빌드와 의존성 설치가 임시 파일을 지우는 일이 잦아 임시 폴더를 넣었다. §7 은 "등록된 프로젝트 폴더 밖"이라고만 적는다. `[§12]` owner 확정 (§12.6 의 9): 임시 폴더를 묻지 않는 루트에 넣는다. 시스템 폴더의 재귀 삭제는 차단의 `system_delete` 가 먼저 받는다.
- 3: 프로젝트 안의 의존성 설치(`npm install` · `uv sync` · 가상 환경 안의 `pip install` · `flutter pub get`)는 프로그램 설치로 보지 않는다. 세션 안의 환경 변수(`export` · `set` · `$env:X =`)도 걸리지 않는다. `HKLM` 의 환경 변수는 차단의 `security_config` 가 먼저 받는다. S-10 확정: `pip install` · `pip uninstall` 은 `--user` 가 있거나, 가상 환경 밖이면(환경 변수 `VIRTUAL_ENV` 가 없고 명령 경로에 `venv` 가 없고 `--target` 도 없다) 묻는다. `uv pip install` 은 `--system` 일 때만 묻는다.
- 5 의 (가): 훅이 그 주소에 HEAD 를 보내 `Content-Length` 를 읽는다(3초. S-10 확정, `HEAD_TIMEOUT`. 루프백 · 사설 주소에는 보내지 않는다). 길이를 알 수 없으면 통과시킨다. `[§12]` owner 확정 (§12.6 의 10): 크기를 모르는 내려받기는 통과시킨다. 묻는 쪽으로 하면 길이를 주지 않는 서버의 내려받기가 모두 결재가 된다.
- 5 의 (나) `known_hosts[]`: 등록된 프로젝트의 git 원격 호스트와 설정의 목록(`Config.studio_known_hosts`. S-10 이 더했다. git 원격 호스트는 프로젝트 폴더의 `.git/config` 에서 읽는다 — 읽지 못한 호스트는 한 번 더 묻게 될 뿐이다)이다. 승인해도 목록은 늘지 않는다 — 같은 주소로 다시 올리면 다시 묻는다. 루프백과 사설 주소는 외부로 보지 않는다 (서버 자신은 `server_control`).
- `git push` 는 올리기지만 1 이 받는다.
- 4 (`power_state`) S-10 확정: 취소로 보는 것은 `/a` · `-a` · `/?` · `--help` 뿐이다. Windows `shutdown` 의 `-c` 는 메시지(주석)라 취소가 아니다 — `shutdown -s -t 0 -c "메시지"` 는 묻는다.

### 10.5 판정 순서와 물어보기

훅은 도구 호출 하나에 아래를 위에서부터 한다.

| # | 걸음 | 결과 |
|---|---|---|
| 1 | `tool` 이 `mcp__studio__*` 다 | 통과 |
| 2 | `guard_ctx.json` 과 환경 변수를 읽는다. 읽지 못하면 | 거부 (`guard_error`) |
| 3 | 명령을 푼다 (§10.2). 풀지 못하면 | 거부 (`unparsed`) |
| 4 | 조각마다 차단 7항목을 §10.3 표의 순서로 본다. 하나라도 걸리면 | 거부. 서버에 묻지 않는다 |
| 5 | 조각마다 물어보기 5항목을 §10.4 표의 순서로 본다. 하나라도 걸리면 | `POST /studio/guard/check` 로 묻고 응답을 따른다 |
| 6 | 아무것도 걸리지 않았다 | 통과 |

- 차단이 먼저다. 체인의 한 조각이 차단이면 다른 조각이 무엇이든 전체를 거부한다 — Claude Code 는 명령을 통째로 실행하거나 통째로 막는다.
- 물어보기가 여럿 걸리면 표에서 먼저 나오는 `rule_id` 하나를 보낸다. `command` 는 명령 전체의 원문이라 owner 는 체인 전부를 보고 결정하고, 승인은 그 명령 전체에 대한 것이다.
- `rule_id` 의 값은 열넷이다: 차단 일곱, 물어보기 다섯, 훅 자신의 `unparsed` · `guard_error`. `POST /studio/guard/check` 에 가는 것은 물어보기 다섯뿐이다. 그 밖의 값이 오면 서버는 결재를 만들지 않고 `deny` 를 돌려준다 (S-10 확정 — `guard.check` 가 `REASONS` 에 없는 `rule_id` 를 먼저 거른다).

**물어보기일 때 — 런 정지와 결재.** 서버의 분기는 §6.4 의 표 넷이고 여기서 바꾸지 않는다.

- 작업 런이고 작업이 `working` 이면(분기 2) system 의 T4 다 (§4.3). `risky_command` 결재가 생기고 작업은 `blocked` 가 된다. payload 의 `command` · `tool` · `rule_id` 는 요청에서 오고, `reason` 은 서버가 `rule_id` 로 고른 문장, `stage` 는 런의 지금 단계다 (§5.3). `cwd` 는 요청에 없어 비운다 — 넣으려면 §6.4 의 요청에 선택 필드를 더한다 (S-10 확정 — `guard.check` 는 `cwd` 를 받지 않고 payload 에 싣지 않는다).
- 응답은 `deny` · `inbox_id` · `end_turn: true` 다. 훅은 `message` 를 거부 이유로 돌려주고(§10.6) 직원은 턴을 끝낸다. 대기 시간(`Config.studio_cut_after`, 기본 60초)이 지나도 돌면 서버가 `RelayEngine.cancel` 로 끊는다 (§6.4, §8.3 의 "서버의 뒤처리". 가드에서 그 진입점을 부르는 곳은 `api.guard_check` 다 — S-10 확정). 기다림 = 런 종료다 (전제 3).
- owner 가 결정하면 T5 다. 결정문(§5.6)이 그 런에 들어가고(§4.7), `approve` 면 1회 허용이 남는다. 직원이 같은 명령을 다시 시도하면 훅이 다시 묻고 서버가 분기 1 로 `allow` 를 준다.
- 작업 런이 아닌 턴(분기 4)과 작업이 `blocked` · `review` 인 구간(분기 3)에서는 결재가 생기지 않고 거부만 된다 (§4.5, §6.4).

**1회 허용.** 지킬 조건은 셋이다 (§5.5 가 정했다):

- (가) 허용은 런 id 가 아니라 작업으로 찾는다. 런이 `done` 으로 끝나 새 런이 `tasks.run_id` 를 바꿔도 남는다 (§4.7).
- (나) 쓰는 것은 작업 런뿐이다 (§6.4 가드의 분기 1).
- (다) 한 번 쓰면 없어지고, T9 · T11 에서 버린다 (§4.3 T9 · T11).

채우는 방법:

- 저장 자리: 승인된 `risky_command` 결재 행의 `inbox.decision_json` 에 `grant` 를 둔다. `approve` 로 결정할 때 서버가 쓴다. 새 테이블 · 컬럼은 없다.

```json
{"grant": {"key": "<sha256 hex>", "used_at": null, "used_run_id": null, "void": false}}
```

- 키: `sha256(tool + "\n" + 공백을 정리한 command)` 의 hex 다. `tool` · `command` 는 그 결재의 payload 값이다. 공백 정리는 앞뒤 공백을 떼고 이어진 공백 · 탭 · 줄바꿈을 한 칸으로 바꾸는 것뿐이다. **같은 명령**은 이 키가 같은 것이다 — 대소문자, 따옴표, 경로 표기, 인자 순서가 다르면 다른 명령이고 다시 결재를 받는다. 결정문이 명령 원문을 직원에게 그대로 주므로(§5.6) 직원은 그대로 다시 실행하면 된다.
- 찾기 (§6.4 가드의 분기 1): `task_id` 가 그 작업이고 `kind = 'risky_command'` · `status = 'decided'` · `decision = 'approve'` 인 행 가운데 `grant.key` 가 같고 `used_at` 이 비었고 `void` 가 `false` 인 것이다. `inbox_task` 색인으로 찾는다 — (가). 요청한 런이 작업 런인지는 §6.4 가 먼저 본다 — (나).
- 쓰기: 찾은 행의 `used_at` · `used_run_id` 를 채운다. 읽고 고치는 것을 `StudioStore` 의 잠금 안에서 한 번에 해서 두 요청이 같은 허용을 함께 쓰지 못하게 한다 — (다). 같은 명령을 두 번 승인받았으면 행이 둘이고 오래된 것부터 쓴다.
- 버리기: T9 와 T7 이 그 작업의 쓰지 않은 허용을 `void = true` 로 닫는다. 둘 다 §4.3 의 부수 효과에 있다 — T9 · T11 은 되돌린 런의 허용을 버리고, T7 뒤에는 쓸 작업 런이 없다 (§5.5). T8 뒤에는 남는다 (owner 확정, §12.6 의 2).
- `decided` 인 결재 행에서 결정 뒤에 바뀌는 것은 셋이다. `delivery` · `delivered_at` 은 결정문을 런에 넣을 때와 T7 · T9 · T11 이 `none` 으로 돌릴 때 바뀌고(§3.13, §4.7), `decision_json.grant` 는 허용을 쓸 때와 닫을 때 바뀐다. `decision` · `decision_comment` 와 `decision_json` 의 나머지 키는 그대로다.
- 서버 메모리에 두지 않는 이유: 재시작하면 사라져, 승인받은 명령이 재시작 뒤 다시 결재가 된다.

### 10.6 훅의 응답 형식

- 기존 훅 `backup_hook.py` 는 막지 않는 훅이다. 어떤 실패에도 exit 0 으로 끝난다 (10 · 54–56행). 도구 호출을 거부하는 훅의 선례는 저장소에 없다.
- 통과: exit 0, 출력 없음.
- 거부: exit 2, stderr 에 거부 문장. Claude Code 가 그 도구 호출을 실행하지 않고 문장을 모델에게 돌려준다고 봤다. **확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "차단 명령이 실제 Claude Code 에서 exit 2 로 막히는지")**: 설치된 Claude Code 에서 PreToolUse 훅의 exit 2 가 실제로 호출을 막는지, stderr 가 모델에게 가는지, stdout 의 JSON 판정 형식을 써야 하는지. 코드를 읽어 확인한 것이 아니라 기억에 기댄 것이다. S-10 확정: 판정기는 이 형식(exit 2 · stderr 에 UTF-8 한 줄 · stdout 없음)으로 만들었고 실제 프로세스 테스트로 고정했다(`tests/test_studio_guard.py`). 설치된 Claude Code 가 그 exit 2 로 호출을 막는지는 사람이 한 번 확인한다 — 아직 하지 않았다.

거부 문장의 모양 (S-10 확정. 상수는 `studio_guard.py` 의 `BLOCKED` · `UNPARSED` · `ERRORED` · `UNREACHABLE`):

```
[가드 · git_risky · 결재 필요] <서버가 준 message>
[가드 · system_delete · 차단] 이 명령은 치명 동작 목록에 걸려 실행하지 않는다. 표기를 바꿔 다시 시도하지 말고, 다른 방법으로 진행하거나 ask_owner 로 물어라.
[가드 · unparsed · 차단] 명령을 해석하지 못해 실행하지 않는다. 변수나 인코딩 없이 명령과 경로를 그대로 적어 다시 실행하라.
```

- 물어보기의 문장은 서버의 `message` 그대로다. `end_turn` 이 `true` 면 §8.3 의 "이제 턴을 끝내라"로 끝난다 (§6.4). 꼬리표는 서버가 결재를 만들었으면(`inbox_id` 가 있다) `결재 필요`, 아니면 `거부` 다. 서버에 닿지 못했으면 `[가드 · <rule_id> · 차단]` 과 "잠시 뒤 그대로 다시 시도하라"이고, `guard_error` 는 `[가드 · guard_error · 차단]` 이다 (S-10 확정). `unparsed` 는 위 문장 뒤에 무엇을 고칠지 한 문장이 붙는다 (S-10 확정. `studio_guard.py` 의 `_why`) — 변수가 있으면 "변수 자리에 실제 값을 적어라", 폴더 이동 대상을 알 수 없으면 "폴더를 옮긴 대상을 실제 경로로 적거나, 그 뒤의 경로를 절대 경로로 적어라", 따옴표 · 괄호가 맞지 않으면 "따옴표와 괄호의 짝을 맞춰라", 셸이 읽을 본문을 펴지 못했으면(파이프 · 인코딩 · 깊이 초과 · heredoc · `iex`) "셸에 넘긴 본문을 명령으로 직접 적어라". 그 밖의 사유에는 붙지 않는다. 판정 · `rule_id` · exit code 는 그대로다.
- 닫힌 쪽으로 실패한다. 훅 안의 예외, 읽지 못한 `guard_ctx.json`, 없는 토큰은 모두 거부다 (`guard_error`). 서버에 물었는데 `allow` 인 200 이 아니면 거부이고, 기다리는 시간은 5초다 (S-10 확정, `ASK_TIMEOUT`). 토큰은 studio MCP 서버와 같이 `Authorization: Bearer <토큰>` 으로 싣는다. §6.4 의 훅 실패 처리와 같고 `backup_hook.py` 와는 반대 방향이다.
- 훅 프로세스가 아예 뜨지 못할 때(파이썬 없음, 파일 없음) Claude Code 가 도구를 통과시키는지는 확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "훅 프로세스가 뜨지 못할 때 Claude Code 가 도구를 통과시키는지"). 통과시킨다면 서버가 런을 만들기 전에 훅을 한 번 돌려 보고 실패하면 런을 만들지 않는 검사가 필요하다. S-10 은 그 검사를 넣지 않았다 — 지금 서버는 런을 만들기 전에 훅을 돌려 보지 않는다. 사람의 확인이 "통과시킨다"로 나오면 더한다.

### 10.7 감사 로그

S-10: "모든 판정을 감사 로그에 남긴다." 통과도 남긴다.

- 자리: `runs/studio/audit/guard-<YYYYMMDD>.jsonl` (§2.2, §3.16). 훅이 판정마다 한 줄을 덧붙인다. 날짜는 서버의 지역 날짜다.
- 훅이 직접 쓰는 이유: 차단과 통과는 서버를 거치지 않는다. 서버가 쓰게 하면 도구 호출마다 HTTP 요청이 하나 는다. DB 에 두지 않는 이유: 훅은 DB 를 열지 않는다 (§3.1).

```json
{"v": 1, "at": "2026-10-10T15:42:23", "run_id": "20261010-154223-00990b", "employee_id": "emp_1a2b3c4d", "room_id": "<rooms.id>", "task_id": "<tasks.id 또는 null>", "mode": "work", "tool": "Bash", "command": "git push origin main", "cwd": "C:\\Projects\\MNYS", "verdict": "ask", "rule_id": "git_risky", "outcome": "denied", "server": {"decision": "deny", "inbox_id": "inb_1a2b3c4d", "end_turn": true}, "ms": 41}
```

| 필드 | 내용 |
|---|---|
| `at` | §3.1 의 시각 형식 |
| `run_id` · `employee_id` · `room_id` · `task_id` · `mode` | 토큰 `claims` 의 `run` · `emp` · `room` · `task` · `mode`. 서명을 검증하지 않은 값이다 |
| `tool` · `command` · `cwd` | §10.2. `command` 는 4000자에서 자르고, 잘랐으면 `truncated: true` 를 더한다 |
| `verdict` | 훅의 판정: `allow` · `block` · `ask` |
| `rule_id` | 걸린 규칙 (§10.5). `allow` 면 `null` |
| `outcome` | 도구 호출의 끝: `allowed` · `denied` |
| `server` | `ask` 일 때 서버의 응답 `{decision, inbox_id, end_turn}`. 닿지 못했으면 `{error}`. 그 밖에는 없다 |
| `ms` | 판정에 걸린 시간 |

- `verdict = ask` 이고 `outcome = allowed` 인 줄이 1회 허용을 쓴 호출이다.
- 한 줄을 덧붙이기 쓰기 한 번으로 쓴다. 여러 훅 프로세스가 같은 파일에 쓰므로 줄이 섞이지 않는지는 S-10 이 확인한다. S-10 확정: Windows 의 덧붙이기는 끝으로 옮긴 뒤 쓰는 두 걸음이라 두 훅이 같은 자리에 쓸 수 있다. 그래서 훅은 파일의 첫 바이트를 잠근 채 쓴다 (`studio_guard.py` 의 `_hold`. 3초 안에 차례가 오지 않으면 쓰지 못한 것이다). 스레드 여섯이 240줄을 함께 쓰는 테스트로 본다. S-10 마무리 확정: 스레드는 한 프로세스 안의 일이라, 훅이 실제로 뜨는 모양도 테스트로 본다 — 프로세스 넷이 `studio_guard` 를 스크립트로 뜰 때처럼 읽고, 준비를 알린 뒤 시작 파일 하나에 맞춰 함께 출발해 160줄을 쓴다 (`tests/test_studio_guard.py`). 빠진 줄도 깨진 줄도 없다.
- 로그를 쓰지 못하면 거부한다. 남기지 못한 판정으로 명령이 실행되지 않게 한다 (S-10 확정 — `studio_guard.main`: 통과로 판정하고도 줄을 쓰지 못하면 `guard_error` 로 거부한다). S-10 확정: `guard_ctx.json` 을 읽지 못한 `guard_error` 는 쓸 자리를 몰라 줄 없이 거부한다. `guard_error` 의 원인은 줄의 최상위 `error` 에 남긴다 (`server` 는 `ask` 의 것이다). `mcp__studio__*` 호출은 통과가 먼저이고 줄은 쓸 수 있을 때만 남긴다 — 쓰지 못해도 통과시킨다. 막으면 직원이 `ask_owner` 로 빠져나갈 길이 없어진다.
- 서버가 남기는 기록은 그대로다 — `risky_command` 결재(분기 2)와 `task_events` 의 `guard` 행(분기 3, §4.5). 검수 패키지의 `incidents[]` 는 `task_events` 에서 온다 (§5.4). 감사 로그는 그것과 별개로 통과까지 담는 전체 기록이다.
- 읽는 REST 경로와 보존 기간은 두지 않았다 (S-10 확정). `studio/runlog.py` 가 `run_id` 로 걸러 읽는다(아래 '런별 모음'). 지우는 코드와 REST 경로는 없다 — 보려면 파일을 직접 열고, 날짜마다 하나씩 쌓인 채 남는다. 엔진의 디스크 집계는 `runs/studio/` 를 런 폴더로 세지 않는다 (`tests/test_disk.py`).
- 비용: 도구 호출마다 파이썬 프로세스가 하나 뜬다. 재지 않았다 — 사람이 감사 로그의 `ms` 로 잰다 (`VERIFICATION.md` 의 S-10 "이름만 적은 `Bash` 사전 승인이 모든 셸 명령을 승인하는지").
- 직원은 이 파일을 고칠 수 없다 (`protected[]`, §10.8).

#### 런별 모음 (S-11 ①)

판정: 헤드리스 `claude -p` 런에서 game-harness 가 자기 기록(훅 · 이벤트 로그 · 스킬 호출 기록)을 남기는지는 **코드로는 판정할 수 없다** — 그 폴더는 이 저장소 밖이고 읽지 못했다. 그래서 릴레이가 직접 보는 것만 런별로 모은다: 러너가 낸 이벤트(`ClaudeCliRunner._emit_activity` 의 `tool_use`(도구 `Skill`) · `mcp_call` · `mcp_result`, §11.2)와 이 절의 감사 로그에서 `run_id` 가 같은 줄. 하네스 폴더는 읽지 않는다. 헤드리스 런이 프로젝트 훅을 실제로 돌리는지는 `VERIFICATION.md` 가 맡는다. S-11 ① 마무리 확정: 다시 봐도 하네스 폴더는 읽지 못했고 판정은 그대로다. 실제 런에서만 확인되며 `VERIFICATION.md` 의 S-11 세 항목이 맡는다.

- 파일: `runs/studio/runlog/<run_id>.json` (`studio/runlog.py`). 런마다 하나, 쓸 때마다 이벤트와 감사 로그에서 전체를 다시 계산해 덮어쓴다 — 런이 여러 번 재개돼도 결과가 같다. 계산과 쓰기는 프로세스 잠금 안에서 한 번에 하나씩 하고, 임시 파일 `<run_id>.json.<pid>.<thread>.tmp` 에 쓴 뒤 `os.replace` 한다. 쓰기가 실패하면 임시 파일을 지우고 기존 파일은 그대로 둔 채 예외를 올린다(`RunSink.idle` 이 잡아 로그로 남긴다). `after_advance` 가 런 잠금 밖이라 같은 런의 idle 이 겹칠 수 있어서 이렇게 했다. `run_id` 는 `^[A-Za-z0-9_-]+$` 만 받는다.
- 쓰는 때: `RunSink.idle`(`engine.after_advance`, `runs.py`) 맨 앞. 정지가 있었든 없든 쓰고, 실패해도 로그만 남기고 결재 올리기는 그대로 간다. `pipeline.py` · `runners.py` 는 고치지 않았다.
- CLI 가 돌린 런은 서버의 `after_advance` 를 타지 않아 파일이 없다. `runlog.read` 는 `None` 을 준다.
- 읽기: `runlog.read(runs_dir, run_id)` (없음 · 깨짐 · 다른 런의 파일이면 `None`). 런이 도는 중에는 저장분이 아니라 `runlog.snapshot(engine, run_id)`(쓰지 않음)로 그 순간 값을 읽는다 — S-11 ② 가 쓴다. REST 경로와 보존 기간은 두지 않는다.

```json
{"v": 1, "run_id": "20261014-…", "collected_at": "2026-10-14T10:00:00",
 "skills": [{"at": "…", "stage": "build", "name": "game-review", "target": "{\"skill\": \"game-review\", …"}],
 "mcp": [{"at": "…", "stage": "build", "server": "mnys", "tool": "query", "target": "…", "chars": 42, "is_error": false}],
 "guards": [{"at": "…", "tool": "mcp__mnys__query", "mcp": true, "rule_id": "…", "verdict": "ask",
             "outcome": "allowed", "command": "…(200자)", "inbox_id": "inb_…"}],
 "counts": {"skills": 1, "mcp_calls": 1, "mcp_errors": 0, "guard_judged": 3, "guard_allowed": 2, "guard_warnings": 1}}
```

| 필드 | 출처와 규칙 |
|---|---|
| `skills[]` | 이벤트 `tool_use` 중 `detail.tool == "Skill"`. `name` 은 `target`(`describe_tool` 이 입력을 JSON 글 200자로 남긴다)에서 `"skill": "…"` 를 정규식으로 뽑는다 — 잘린 JSON 도 읽힌다. JSON 이 아니면 첫 낱말(앞의 `/` 제거). 못 뽑으면 `""` 이고 `target` 원문이 남는다 |
| `mcp[]` | 이벤트 `mcp_call`. `mcp_result` 는 같은 (단계, 서버, 도구)의 결과 없는 가장 오래된 호출에 `chars` · `is_error` 를 채운다. 결과가 없으면 `null` |
| `guards[]` | 감사 로그에서 `run_id` 가 같고 `verdict != "allow"` 인 줄. `mcp` 는 도구 이름이 `mcp__` 로 시작하는지. `inbox_id` 는 줄의 `server.inbox_id`, 없으면 `null`. 이 저장소에서 MCP 가드 경고의 원천으로 확인되는 것은 감사 로그뿐이다 — `defaults/mcp_guards.json`(지금 `{}`)이 내는 경고가 릴레이 이벤트로 오는 길은 확인하지 못했다 |
| `counts` | `guard_judged` = 그 런의 감사 줄 수, `guard_allowed` = 그 가운데 allow. 통과 줄은 개수만 센다(도구 호출 수만큼 커지지 않게) |

감사 로그를 읽을 때 `run_id` 앞 8자가 날짜이면 그 전날 이후 파일만 연다. 줄에 `run_id` 글이 있을 때만 파싱하고, 깨진 줄과 못 읽는 파일은 건너뛴다.

### 10.8 가드 파일과 서버 설정의 보호, 남는 구멍

S-10: "가드 파일 자체와 서버 설정은 직원이 못 고친다." `protected[]`(쓰기 · 삭제 · 이동을 막는다)와 `secrets[]`(읽기도 막는다)가 받는다.

| 대상 | 경로 | 막는 것 |
|---|---|---|
| 가드 파일 | 서버 루트의 `relay_agent/studio_guard.py` | 쓰기 (`protected_write`) |
| 서버 코드 | 서버 루트의 `relay_agent/` 전체 — 가드를 거는 `pipeline.py` · `runners.py`, 검사를 하는 `relay_agent/studio/`, 위의 가드 파일 | 쓰기 (`protected_write`) |
| 훅 설정과 판정 재료 | 런 폴더의 훅 설정 파일 · `guard_ctx.json` (§11.4) | 쓰기 |
| 서버 설정 | 서버 루트의 `relay.config.yaml` · `relay.config.local.yaml` (`config.py` 59–63) | 쓰기. `relay.config.local.yaml` 은 읽기도 (`secret_access`) |
| studio 데이터 | `runs/studio/` 전체 — DB, 직원 폴더, 정책, 초안, 첨부, 감사 로그 | 쓰기 |
| 다른 런의 토큰 | `%TEMP%\katae-*\mcp.json` | 읽기 (`secret_access`) |

- `runs/studio/` 전체를 넣은 이유: 직원이 DB 를 고치면 결재를 스스로 결정할 수 있고, 직원 폴더를 고치면 자기 지침과 상한을 바꿀 수 있고(§9.6), 감사 로그를 고치면 흔적이 지워진다. 가드와 결재가 그 위에 서 있다 (전제 2). 직원이 이 폴더에 직접 써야 하는 일은 없다 — 첨부는 경로를 넘기면 서버가 복사한다 (§6.6). `[§12]` owner 확정 (§12.6 의 11): `runs/studio/` 전체의 쓰기를 막는다.
- `relay_agent/` 전체를 넣은 이유: 가드를 거는 `pipeline.py` · `runners.py`(§11.4)와 검사를 하는 `relay_agent/studio/` 가 거기 있다. 쓰기를 막는 규칙은 `protected[]` 뿐이라(§10.3 의 6. §10.4 에는 프로젝트 밖 쓰기를 묻는 규칙이 없다) 넣지 않으면 다른 프로젝트에 배치된 직원도 절대 경로로 이 코드를 쓸 수 있다. `copy` 런의 복사본은 경로가 달라 걸리지 않는다 — 서버 코드를 고치는 작업은 복사본에서 하고 owner 가 통과시킨다 (아래). `runs/studio/` 와 같은 종류의 추가다 (S-10 확정 — `guard.write_ctx` 가 `protected[]` 에 넣는다).
- 읽기는 막지 않는다. 다른 직원의 지침이나 DB 를 읽는 것은 치명 동작이 아니다.
- 막는 것은 훅의 거부이지 OS 권한이 아니다. 서버 자신의 쓰기(REST, 결재 승인, H-4)는 훅을 지나지 않는다.
- studio-server 저장소의 작업은 `copy` 로 돈다 (P-4). 복사본 안의 `relay_agent/`(가드 파일 포함)는 다른 경로라 고칠 수 있고, 원본에 닿는 것은 owner 가 통과시킨 뒤다 (T7). 이것은 런이 실제로 `copy` 로 만들어질 때의 이야기다 — 읽기 전용이 아닌 MCP 가 하나라도 붙으면 엔진이 `copy` 를 원본 작업으로 바꾸고 이벤트 `workspace_forced` 를 남긴다 (`pipeline.py` 581–586 · 629–630). 그래서 `studio` 를 `READ_ONLY_MCP` 에 더하고(§11.2), 그것으로 모자란 자리는 두 겹으로 받는다 (아래 표의 8). 원본 작업으로 도는 런이어도 서버 루트의 `relay_agent/` 는 훅이 막는다 (위 표). 서버 코드를 품은 프로젝트의 작업 런은 T3 가 원본 작업으로 만들지 않는다 (§4.3 T3).

- git 이 대신 쓰는 것에서 남는 것 (H-3 마무리. 막는 쪽은 §10.3 의 '6 의 git'). 첫째, pathspec 없이 트리 전체를 바꾸는 git — 서버 루트에서의 `git checkout <옛 커밋>` · `stash` · `pull` · `merge` · `switch` — 은 막지 않는다. 디스크의 `relay_agent/studio_guard.py`(훅 자신)가 옛 판으로 바뀔 수 있다. 막으려면 훅이 저장소 꼭대기를 디스크에서 찾아야 하고, 막는 범위는 owner 가 정한다 — 확인 필요 (사람). 둘째, 환경 변수로 저장소를 가리키는 것(`GIT_DIR=… GIT_WORK_TREE=… git …`, `export GIT_DIR`), `git apply` · `am` 의 패치 속 경로, `--pathspec-from-file` 의 파일 내용은 읽지 않는다 (일하는 폴더가 보호 경로의 상위면 `--pathspec-from-file` 자체를 막는다). 셋째, 하위 명령 목록(`GIT_READ` · `GIT_TREE` · `GIT_PLAIN`)은 손으로 적은 것이다 — 보호 폴더 안은 허용 목록이라 빠진 이름이 막히는 쪽으로 틀리고, 밖은 `GIT_PLAIN` 에 든 이름이 경로로 쓰는 옵션을 새로 얻으면 놓친다.

남는 구멍 — 훅으로 막지 못하는 것:

| # | 구멍 | 지금의 대책 |
|---|---|---|
| 1 | 스크립트 안의 동작. `python x.py` · `node x.js` · 빌드 스크립트가 안에서 하는 삭제 · 전송 · 서버 요청은 보이지 않는다. `python -c "…"` 도 파이썬을 해석하지 않는다 | 한 줄 본문에 보호 경로나 서버 주소가 글자 그대로 있으면 잡는다. S-10 확정: 서버 주소는 모든 인자에서 찾고(§10.3 의 5), 경로는 인터프리터(`python` · `py` · `pypy` · `node` · `bun` · `deno` · `ruby` · `perl` · `php`) 명령의 인자에서 따옴표 안 문자열 가운데 경로처럼 보이는 것(드라이브 · `/` · `~` · `./` 로 시작하거나 글자 사이에 `/` · `\` 가 있다)을 `secret_access` 와 `protected_write` 가 본다. 본문을 해석하지 않으므로 읽기와 쓰기를 가리지 않는다 — 보호 경로를 읽기만 하는 한 줄도 막힌다(`cat` · Read 는 된다). 잡지 못하는 것: 이름만 적은 파일(`open('pipeline.py','w')`), 이어 붙인 경로, 변수에 든 경로, 보호 경로의 상위를 지우는 한 줄(`shutil.rmtree('<서버 루트>')`). 스크립트 파일(`bash x.sh` · `-File x.ps1` · `python x.py`)은 열지 않는다. 그 이상은 없다 (CAMPAIGN.md §8 의 위험 1) |
| 2 | MCP 도구 안의 동작 — 에디터에서 실행하는 코드 등 | 인자의 경로만 본다 (§10.2) |
| 3 | 서버를 직접 부르는 길 둘 (§6.2). (가) `/studio` 가 아닌 기존 경로(`/admin/restart` · `/runs/{id}/approve` 등)는 루프백을 토큰 없이 믿는다. (나) `/studio/*` 는 루프백에도 토큰을 요구하지만(owner 확정, §12.6 의 5) 공용 토큰을 읽어 낸 스크립트는 owner 로 통한다. 셸의 요청은 `server_control` 이 막지만 1 의 스크립트는 막지 못한다 | 토큰이 있는 자리를 막는다 — `relay.config.local.yaml` 과 브라우저 프로필(대시보드가 `localStorage` 에 둔다)은 `secret_access`, `relay remote status` 는 `server_control` 의 (다), 환경 변수 `KATAE_TOKEN` 은 4 다. 스크립트 안에서 그 자리를 읽는 것은 남는다 (1). (가)의 기존 경로는 그대로 남는다 |
| 4 | 공용 토큰의 상속. 러너는 `call.env` 가 있으면 `{**os.environ, **call.env}` 를, 없으면 서버의 환경을 그대로 CLI 프로세스에 준다 (`runners.py` 271). 서버가 `KATAE_TOKEN` 을 환경 변수로 받았다면(`config.py` 45) 직원의 셸에서 읽힌다. `/studio/*` 가 루프백에도 토큰을 요구하므로 이 토큰이 직원이 자기 결재를 스스로 승인하는 것을 막는 마지막 문턱이다 (3 의 나) | 서버를 `KATAE_TOKEN=<값> uv run relay serve` 로 띄우면 그렇게 된다 (`README.md` 38행, 읽음). 이 PC 의 서버가 그 방식으로 뜨는지는 보지 않았다 — 확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "studio 런의 셸에서 서버의 `KATAE_TOKEN` 이 읽히지 않는지"). studio 런의 `StageCall.env` 에서 `KATAE_TOKEN` 을 빈 값으로 덮는다. S-10 확정: `api.guard_stage` 가 서버 환경에 `KATAE_TOKEN` 이 있을 때만 env 에 `KATAE_TOKEN` 을 빈 값으로 넣는다 — 없으면 키를 더하지 않는다. 그 런의 CLI · 셸 · MCP 서버 프로세스가 빈 값을 물려받는다. `katae` MCP(`mcp_servers/katae.py` 28–33행)는 토큰이 비면 `Authorization` 헤더를 보내지 않고, 이 PC 의 서버는 루프백의 기존 경로를 토큰 없이 받으므로(3 의 가) 그대로 닿는다 — 테스트로 본다. S-10 마무리 확정 — 빈 값과 없음은 결과가 다르다. `Config.load` 가 `os.environ.get("KATAE_TOKEN", cfg.auth_token)` 이라(`config.py` 80) 변수가 빈 값으로 있으면 파일의 `auth_token` 도 쓰이지 않는다. 변수가 없을 때만 파일의 값이 쓰인다. 다섯 가지로 나눈다. (가) 빈 값이 전해질 때: `katae` MCP 는 헤더를 보내지 않는다. 이 PC 의 서버에는 닿고, `KATAE_URL` 로 가리킨 다른 PC 의 서버는 401 이다 — 테스트로 본다. (나) 변수가 없을 때 — 서버가 `KATAE_TOKEN` 을 환경 변수로 받지 않아 `api.guard_stage` 가 키를 더하지 않은 경우다: `katae` MCP 는 `relay.config.local.yaml` 의 `auth_token` 을 보낸다 — 테스트로 본다. 그 토큰은 MCP 프로세스 안에만 있고 직원의 셸 환경에는 없다. 그 파일은 `secret_access` 다. `katae` 도구가 부르는 것은 이 PC 에서 토큰 없이도 닿는 기존 경로라(3 의 가) 권한이 늘지 않는다. `katae.py` 의 토큰 읽기는 그대로 둔다 — 바꾸면 다른 PC 에서 쓰는 방법(`README.md` 48행)이 깨진다. (다) 다른 PC 를 가리키는 `katae` MCP 의 대책: 그 MCP 항목의 `env` 에 `KATAE_URL` 과 그 PC 의 `KATAE_TOKEN` 을 함께 적는다. 코드로 확인된 범위는 셋이다 — 엔진이 프로젝트 `.mcp.json` 의 항목을 `env` 째로 `StageCall.mcp_overrides` 에 싣는다, 러너가 그것을 그대로 런의 `mcp.json`(`%TEMP%/katae-*/`)에 써 `--strict-mcp-config` 와 함께 넘기고 CLI 프로세스는 `KATAE_TOKEN` 이 빈 값인 환경으로 띄운다(`runners.py` 667–677 · 271), 그 토큰이 MCP 프로세스의 환경에 있으면 401 이 풀린다(`katae.py` 를 그 환경으로 읽은 테스트). 사람이 확인할 범위: Claude Code 가 MCP 프로세스를 띄울 때 항목의 `env` 가 물려받은 빈 값을 덮는지는 이 저장소의 코드로 볼 수 없다 (`VERIFICATION.md`). 덮지 않으면 이 대책은 통하지 않는다. (라) Windows 의 빈 값: `{**os.environ, "KATAE_TOKEN": ""}` 로 띄운 자식 프로세스와, 그 자식이 환경을 따로 주지 않고 띄운 손자 프로세스가 모두 빈 문자열을 본다 — 없는 것으로 바뀌지 않는다. 파이썬 프로세스로 본 테스트다. Claude Code 와 그 셸이 같은지는 사람이 확인한다 (`VERIFICATION.md`). 없는 것으로 전해져도 (나)가 되므로 직원의 셸에서 서버의 토큰이 읽히지 않는 것은 같다. (마) 남는 것: 항목의 `env` 에 적은 토큰은 그 항목이 적힌 파일에 글자 그대로 있다. `~/.claude.json` 과 런의 `mcp.json` 은 `secrets[]` 에 있지만(`studio_guard.py` 의 `default_secrets`) 프로젝트 폴더의 `.mcp.json` 은 없어 직원이 읽을 수 있다 (코드로 읽었다. 테스트는 없다). 다른 PC 의 토큰은 `~/.claude.json` 쪽 항목에 적는다 |
| 5 | 행위자 토큰이 직원의 셸에서 보인다 (`STUDIO_ACTOR_TOKEN`) | 자기 토큰이라 권한이 늘지 않는다. 그 토큰으로 서버를 직접 부르는 것은 `server_control` 이 막고, 닿아도 서버의 검사는 같다 (§8.2) |
| 6 | 상위 폴더를 재귀로 읽어 파일 하나짜리 비밀(`relay.config.local.yaml`, 자격 증명 파일)이 결과에 섞이는 것 | 비밀 폴더의 상위만 막는다 (§10.3 의 3) |
| 7 | 프로젝트 폴더의 Claude Code 설정(`.claude/settings*.json`)이 `--settings` 로 넘긴 훅을 끌 수 있는지 | 확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "프로젝트의 `.claude/settings.json` 이 `--settings` 로 넘긴 가드 훅을 끌 수 있는지") |
| 8 | 가드를 거는 코드(`pipeline.py` · `runners.py` 의 연결 지점)와 검사를 하는 `relay_agent/studio/` 가 서버 저장소 안에 있고, 그 저장소가 직원의 작업 대상이 될 수 있다 | 세 겹으로 받는다. (가) 서버 루트의 `relay_agent/` 전체를 `protected[]` 에 넣는다 (위 표. S-10 확정 — `guard.write_ctx`) — 어느 프로젝트의 런이든, 원본 작업이든 훅이 쓰기를 막는다. (나) 서버 코드를 품은 프로젝트의 작업 런은 T3 가 원본 작업으로 만들지 않는다 (§4.3 T3 의 `copy` 고정 인자. S-8 확정) — `inplace` 는 거부하고, `none` 은 파일을 고치는 단계가 하나도 없을 때만 통과시키며, 고치는 단계가 있는데 `none` 이면 `inplace` 와 똑같이 거부한다. studio-server 저장소를 `copy` 로 둔다는 P-4 는 저장소 설정만으로 유지되지 않는다 — 읽기 전용이 아닌 MCP 가 하나라도 붙으면 엔진이 `copy` 를 원본 작업으로 바꾼다 (`pipeline.py` 581–586, 이벤트 `workspace_forced` 629–630). (나)는 그때 런을 만들지 않는다. (다) `studio` 를 `READ_ONLY_MCP` 에 더한다 (§11.2. S-6 확정) — 더하지 않으면 (나)가 이 저장소의 작업을 모두 거부한다. 더한 뒤에도 릴레이 단계의 `mcp`, 직원의 `employee.yaml` 의 `mcp`, studio-server 폴더에서 찾은 프로젝트 MCP 에 읽기 전용이 아닌 것이 있으면 그 작업은 원본에서 도는 대신 (나)에 걸려 시작되지 않는다. 남는 것은 셋이다. 스크립트 안의 쓰기(1 의 구멍)는 `relay_agent/` 에도 닿는다 — 고친 코드는 서버를 다시 띄울 때 읽히고, 훅이 호출마다 새 프로세스로 띄우는 `studio_guard.py` 는 바로 읽힌다. `relay_agent/` 밖의 서버 루트 파일(`relays/` 등)은 위 표에 든 것 말고는 막지 않는다. 훅이 없는 대시보드 · CLI 런은 어느 겹에도 걸리지 않는다. 런의 실제 방식은 `RunState.workspace_mode` 에 남는다 (§4.3 T3) |
| 9 | Claude Code 가 아닌 러너에는 PreToolUse 훅이 없다. `settings_path` 는 Claude Code 의 `--settings` 다 (`runners.py` 62 의 주석). 릴레이의 `alternates`(`pipeline.py` 93)나 `Config.fallback_chain`(`config.py` 29)이 단계를 다른 AI 로 넘기면 가드 없이 돈다 | 단계에 쓰일 provider 를 세 길에서 Claude Code 로 묶는다 — `employee.yaml` 의 칸(§9.4 의 8), 이번만 덮어쓴 칸, 릴레이 파일이 단계에 적은 provider. 뒤의 둘은 §9.5 의 5 가 보고, 걸리면 `start` 를 `provider_unguarded` 로 거부한다. `/studio/models` 도 Claude Code 의 항목만 내준다 (§6.4). 남는 것은 실행 중의 대체다. 길은 셋이고 모두 `_run_stage`(`pipeline.py` 1421–1434)를 지난다 — 단계의 `alternates`(1424), 칸이 provider 를 바꿨을 때 첫 대체가 되는 릴레이 자신의 AI(1425–1426), `ProviderRegistry.candidates`(`providers.py` 184–200)가 모든 단계에 덧붙이는 `fallback_chain`(186). `candidates` 에는 덧붙이기를 끄는 인자가 없다. studio 런이면 `_run_stage` 가 `candidates` 가 돌려준 후보에서 `kind` 가 `claude_cli` 가 아닌 것을 뺀다 (S-10 확정 — `StageCall.claude_only` 가 참이면 `_run_stage` 가 `RelayEngine._hookable` 로 거른다. §11.4 의 연결 지점 6). 남는 후보가 없으면 그 단계는 `RunnerError`(`unavailable`)다(1433–1434). 단계 실행 자리의 `except`(1705)가 받는다. 취소(1706–1707) · 단계 예산(1708–1732) 갈래에 들지 않는다. 단계가 `optional` 이면 이벤트 `stage_skipped` 를 남기고 다음 단계로 넘어가고(1733–1737), 아니면 `_fail` 로 런을 끝낸다(1738). `_fail`(1361–1367, 읽음)은 런의 `status`(기본 `failed`. 취소 갈래만 `cancelled` 를 넘긴다)와 `error`(`[단계] 메시지`)를 적고, 이벤트 `run_failed`(취소면 `run_cancelled`)를 남기고, `_write_stop` → `_settle_workspace` → `save` 를 한 뒤 런을 돌려준다 (§11.2). 그래서 §4.7 표의 `failed` · `working` 행이 받아 T4 를 한다. `optional` 단계는 런이 실패하지 않은 채 빠지므로, 건너뛴 것을 작업 기록에 남긴다 (§4.7). 엔진의 `is_claude`(1442–1443)는 `anthropic_api` · `mock` · `kind` 가 `api` 인 것도 참이라 이 판정에 쓰지 않는다. S-10 확정: 테스트의 `mock` 은 `_hookable` 이 `claude_cli` 와 함께 남긴다 — 도구를 부르지 않는 러너다. 다른 러너가 설정 파일을 무시하는지는 주석으로만 봤다. studio 런은 `claude_only` 라 다른 러너로 넘어가지 않으므로 확인할 것이 없어졌다 (S-10 확정) |
| 10 | `--safe-mode`. `isolate` 이고 `project` 가 아닌 단계는 MCP 도 설정 파일도 없으면 `--safe-mode` 가 붙어 훅이 꺼진다 (`runners.py` 591–593) | studio 런은 항상 `studio` MCP 와 훅 설정 파일을 가지므로 해당하지 않는다. S-10 이 테스트로 지킨다. 게임 프로젝트 런과 작업 런은 `isolate` 가 풀려 어느 플래그도 붙지 않는다 (§10.1) |

---

## 11. 기존 코드 접점

"확인" 열: **읽음** = 그 행 범위를 읽고 썼다. **일부** = 시그니처나 검색된 줄만 봤다. **확인 필요** = 읽지 않았다.

### 11.1 저장

| 기존 코드 | 위치 | studio 가 쓰는 방식 | 확인 |
|---|---|---|---|
| `HistoryStore.__init__` | `history.py` 72–85 | `StudioStore` 가 같은 형태를 따른다. 다른 점은 §3.1 | 읽음 |
| `HistoryStore._exec` · `_rows` | `history.py` 87–95 | 같은 이름, 같은 반환(커서 · `list[dict]`) | 읽음 |
| `_now` | `history.py` 68–69 | 시각 형식 (§3.1) | 읽음 |
| `SCHEMA` (`sessions` · `turns` · `events`) | `history.py` 19–61 | 고치지 않는다. `session_id` · `run_id` 로 가리키기만 한다. `turns.run_id` 는 UNIQUE 다 | 읽음 |
| `HistoryStore.create_session` | `history.py` 98 | 직원×방 세션을 만들 때 (S-7) | 일부 |
| `HistoryStore.add_event` · `events` | `history.py` 182–186 · 232–236. `pipeline.py` 691 · 374–375(`_event`)에서 적는다 | 런 이벤트를 읽어 결재로 옮길 때, 건너뛴 단계를 작업 기록으로 옮길 때 (§4.7). 읽는 쪽은 `history.events(run_id, after_id=0)` 이다 (`pipeline.py` 1156 에서 호출) — 그 런의 이벤트를 `id` 순으로 돌려주고 행마다 `id` · `at` · `stage` · `kind` · `detail`(dict)을 가진다 | 읽음 (`history.py` 182–186 · 232–236, `pipeline.py` 374–375). 691 · 1156 은 일부 |
| `Config.runs_dir` · `history_db` · `relays_dir` | `config.py` 18 · 20 · 21 | `runs/studio` 의 기준 경로. studio 경로는 설정 필드가 아니라 `runs_dir` 에서 유도하는 프로퍼티다: `Config.studio_dir` · `studio_db` (`config.py` 75–82) | 읽음 |
| `Config` (필드 추가 자리) | `config.py` 17– | 한도 설정 (§3.16). 읽는 곳은 `Config.load`(58–): `relay.config.yaml` 위에 `relay.config.local.yaml` 을 `_merge` 로 덮고(61–65) `model_validate` 로 검증한다(66). `KATAE_TOKEN` · `KATAE_HOST` 환경 변수가 토큰과 호스트를 덮는다(67–68). `_merge`(87–91)는 겹친 키의 양쪽 값이 모두 dict 일 때만 재귀로 합치고, 그 밖에는 덮는 쪽 값으로 바꾼다 | 읽음 (17–72 · 87–91행) |
| `Config.auth_token` | `config.py` 46 | 공용 토큰. 기기별 토큰과의 관계는 §6 | 읽음 |
| `CampaignRunner._path` · `load` · `save` | `campaign.py` 124–139 | 기존 캠페인은 `runs/campaigns/<cid>.json` 에 임시 파일 + `os.replace` 로 저장하고 `stop_requested` 를 지킨다. studio 캠페인은 이 저장을 쓰지 않는다 — `campaigns.runner_id` 는 `NULL` 로 둔다 (S-14 확정, §3.9) | 읽음 |
| `Campaign` · `Task` 모델 | `campaign.py` 39–82 | 상태 대응은 아래 표 | 읽음 |
| `RepoRegistry.get` · `match` | `repos.py` 138–151 | `projects.repo` 와 `path` 검사 (§3.3) | 읽음 |
| `RepoSpec` | `repos.py` 30–54 | 워크스페이스 방식 · verify · MCP 는 여기 것을 쓴다. `resolved`(52–54)는 `path` 를 `expanduser().resolve()` 한 `Path` 를 돌려주고, `path` 가 비어 있으면 `None` 이다 | 읽음 |
| `engine.repos` | `server.py` 203 | 서버가 가진 `RepoRegistry` | 일부 |

기존 캠페인 상태와의 대응 (S-14 확정). 두 쪽의 상태가 같은 뜻인 곳을 적은 표다 — 코드가 옮겨 적는 것은 없다. studio 캠페인은 `Campaign` · `Task` 모델을 만들지 않고 `campaigns` · `tasks` 테이블의 상태만 쓴다 (§4.10):

| `campaign.py` | studio | 비고 |
|---|---|---|
| `Campaign.status` `planning` | 없음 | studio 캠페인은 플래너 없이 승인된 작업 목록을 쓴다 |
| `running` · `waiting` | `running` | `waiting` 의 이유는 `Campaign.reason` · `wait_until` 에 있다 |
| `paused` · `failed` | `paused` | |
| `done` · `cancelled` | `done` · `cancelled` | |
| `Task.status` `pending` | `backlog` | |
| `running` | `working` (런이 멈춰 있으면 `blocked`) | |
| `done` | `review` | 기존 캠페인에는 검수 단계가 없다 (CAMPAIGN.md 485행). `done` 으로 바로 가지 않는다 |
| `failed` | `blocked` | T4 의 "런이 `failed` 로 끝남" |
| `skipped` | 대응 없음 | 건너뛰기에 맞는 작업 상태가 없다 `[§12]` |

### 11.2 실행

| 기존 코드 | 위치 | studio 가 쓰는 방식 | 확인 |
|---|---|---|---|
| `RelayEngine.create` | `pipeline.py` 501–506 | T3 의 런 생성. 인자 대응은 아래 표. `auto_apply` 인자(502)는 `None` 일 때만 `RepoSpec.auto_apply` → `RelaySpec.auto_apply`(157) 순으로 정해진다(587–588). studio 는 `False` 를 명시한다 (§4.3 T3) | 일부 (시그니처 · 설명 · 587–588행) |
| `stage_models` 검사 | `pipeline.py` 562–580 | 단계별 값에 `provider` · `model` · `effort` 를 받는다. `effort` 는 `providers.efforts_for` 로 검사하고 틀리면 `ValueError` | 일부 |
| `create` 의 `auto` 갈래 · `_route` | `pipeline.py` 543–551 · 634–654, `router.py` 35–53 · 104–122 | 릴레이에 `router` 가 있으면 `create` 안에서 릴레이를 고르고(546–549), 고른 릴레이에 없는 `stage_models` 키를 버린다(550–551). `_route` 는 비공개다. studio 는 이 갈래를 타지 않는다 — 공개 라우팅 메서드 `RelayEngine.route`(S-8 확정)를 더해 런을 만드는 순간에 먼저 고르고, 고른 경로로 `create` 를 부른다 (§9.5). 공개 메서드는 `_route` 가 받는 것에 제외 목록을 더 받아 `spec.router.exclude`(640)에 합치고, `_route` 와 같은 `{path, relay, reason, by, usage, from}` 을 돌려준다 | 읽음 |
| `_run_mcp_names` · `_attached_mcp` | `pipeline.py` 466–476 · 478–491, 쓰는 줄 581–586 · 1653 | 앞은 런을 만들기 전에 붙을 MCP 를 미리 세는 목록이다 — 읽기 전용(`READ_ONLY_MCP`)이 아닌 것이 하나라도 있으면 `copy` 를 원본 작업으로 바꾼다(584–586). 뒤는 단계에 실제로 붙이는 목록이다(1653). 둘 다 프로젝트에서 찾은 MCP 를 `mcp` 인자와 무관하게 넣는다(476 · 482). `create` 에 MCP 한정 인자 `mcp_only`(S-7 확정)를 더해 둘이 같은 값으로 거르게 한다. 채팅 · 읽기 턴이 쓴다 (§9.5). `studio` 는 `READ_ONLY_MCP`(44행: `docs_read` · `handoff` · `digest`)에 없다 — 그대로면 `mcp` 에 `studio` 를 넣은 작업 런이 모두 `copy` 에서 원본 작업으로 바뀌어, P-4 의 `copy`(§10.8 의 8)와 T7 의 `copy` 갈래(§4.3)가 서지 않는다. `studio` 를 `READ_ONLY_MCP` 에 더했다 (S-6 확정 — 지금 44행은 넷이다). 이 목록은 두 곳에서 더 쓰인다 — `_check_mcp_ready` 가 준비 검사(`mcp_probe`)의 대상에서 빼고(1041), `_check_mcp_saved` 가 '바꾸는 MCP 도구를 불렀는데 파일은 바뀌지 않았다'는 경고의 대상에서 뺀다(1073). `studio` 를 더하면 두 곳에서도 빠진다 — 단계를 시작하기 전에 studio MCP 가 응답하는지 엔진은 확인하지 않는다. `create` 에 `copy` 고정 인자 `pin_copy`(S-8 확정. `RunState` 에는 남기지 않는다)도 더한다 — 준 런은 584–586 의 갈래에서 원본 작업으로 바꾸는 대신 `ValueError` 로 거부하고, 방식이 처음부터 `inplace` 여도 거부한다. `none` 은 파일을 고치는 단계가 하나도 없을 때만 통과하고, 고치는 단계가 있는데 `none` 이면 `inplace` 와 똑같이 거부한다 — `none` 런은 `Workspace` 없이 원본 폴더에서 돈다(382–384 · 1551). 서버 코드를 품은 프로젝트의 작업 런이 쓴다 (§4.3 T3) | 읽음 |
| `workdir` 필수 | `pipeline.py` 539–542 | `workdir` 도 `repo` 도 세션의 작업 폴더도 없으면 `ValueError` 다. 채팅 턴의 작업 폴더를 §9.5 가 정한 이유다 | 읽음 (536–542행) |
| `cap_effort` 와 상한 적용 지점 | `pipeline.py` 73–76, 1613–1629 | `model_cap` · `effort_cap` 은 릴레이 기본값과 재시도에만 걸리고 `stage_models` 로 고른 값에는 걸리지 않는다(1623–1627 의 주석과 코드). S-8 은 덮어쓴 값의 상한이 `model_cap` / `effort_cap` 이라고 적는다. studio 가 `create` 를 부르기 전에 칸을 상한과 비교한다 — 이번만 덮어쓴 칸이 넘으면 `start` 를 거부하고 `employee.yaml` 의 칸은 낮춘다 (§9.5. S-8 확정) `[§12]` | 읽음 (1606–1631행) |
| `RelayEngine.advance` | `pipeline.py` 1488–1509 | `advance(run_id, resume=False, cancel=None) -> RunState`. `cancel` 은 이 런의 취소 이벤트다 — 부르는 쪽이 준 이벤트가 진입 전에 켜져 있으면 단계를 돌리지 않고 런을 `cancelled` 로 남긴다 (studio 의 슬롯이 준다, §4.6). 끝(1509)에서 `_let_go`(클래스 끝)가 런 잠금을 놓고 `after_advance(run_id)` 를 부른다 — studio 가 런의 정지를 결재로 올리는 자리다 (§5.8 나, S-5 가 더했다). 런이 끝나거나 멈출 때까지 돌고 그때의 런을 돌려준다. 이미 진행 중이면 `ValueError`. 예상 못 한 예외가 나면 런을 `failed` 로 끝낸다. 돌아올 때 폴더 락을 놓는다. studio 의 슬롯은 이 호출 구간과 같다 (§4.6) | 읽음 |
| `_advance_bg` | `server.py` 187–194 | 데몬 스레드로 `engine.advance` 를 부르는 기존 형태. studio 는 런이 멈춘 뒤의 처리(§4.7)가 필요해 자기 스레드에서 부른다. 동시 실행 한도는 없다 | 읽음 |
| `RelayEngine._advance` | `pipeline.py` 1620–1679 | `resume` 이면 `RESUMABLE` 상태에서, 아니면 `pending` 에서만 진행한다. `inplace` 면 폴더 락을 잡고, 못 잡으면 런을 `failed` 로 끝낸다(1630–1637). 워크스페이스가 준비돼 있지 않으면(`prepared` 가 False) `prepare` 한다(1652–1664) — 처음 시작할 때와, 정리된 뒤 재개할 때다. 단계를 시작하기 전에 `_check_mcp_ready` 를 부른다(1677–1679, 아래 행) | 읽음 |
| `RelayEngine._check_mcp_ready` | `pipeline.py` 1107–1137, 호출 1677–1679 | 남은 단계가 쓰는 MCP(읽기 전용 제외)를 `mcp_probe` 로 확인하고 실패 사유를 돌려준다. `mcp_probe` 가 없으면 검사하지 않는다(1122–1123). 실패하면 호출부가 단계를 시작하지 않은 채 런을 `failed` 로 끝낸다. studio 의 에디터 검사(§4.3 T3) 뒤에 한 번 더 걸리는 검사다 — studio 쪽은 에디터를 켜고 기다린 뒤 안 되면 결재로 묻고, 이쪽은 켜지도 기다리지도 않고 한 번 확인한다 | 읽음 |
| `RESUMABLE` | `pipeline.py` 228 | `pending` · `failed` · `cancelled`. `done` 이 없다 (§4.7) | 읽음 |
| `RelayEngine.approve` | `pipeline.py` 656–671 | T5 에서 `awaiting_approval` 런을 `pending` 으로 돌린다. 예산이면 한도를 늘린다(661–667). 다른 상태면 `ValueError` | 읽음 |
| `RelayEngine.interject` | `pipeline.py` 673–692 | T5 · T8 의 결정문 전달(§4.7), owner 가 방에 쓴 글의 끼어들기(S-9). `done` 런과 빈 글은 `ValueError`(679–683). live 로 전달했든 아니든 `notes.jsonl` 에 적는다(687–688). 돌려주는 `applied` 는 `live` · `next_stage` · `on_resume` | 읽음 |
| `RelayEngine._merge_notes` | `pipeline.py` 694–707 | `notes.jsonl` 에서 `RunState.notes_seen` 뒤의 줄을 `live` 표시와 무관하게 baton 의 `user_notes` 에 더한다. 도는 런에 결정문을 넣지 않는 근거 (§4.7) | 읽음 |
| `RelayEngine.cancel` | `pipeline.py` 790–806 | T4 뒤에도 도는 런 끊기, T3 실패 시 정리, T7 · T9 · T11 앞의 런 정리(§4.3). 스레드가 쥔 런이면 취소 표시만 하고 그때의 런을 그대로 돌려준다(792–797) — 그 스레드가 단계 사이(1574–1575)나 단계 실행 중(1706–1707)에 `cancelled` 로 적는다. 아무도 쥐지 않은 런은 바로 `cancelled` 로 적고 변경을 모은다(798–805). `done` · `cancelled` 런은 `ValueError` | 읽음 |
| 폴더 락 `_folder_lock` · `folder_holder` · `_acquire_folder` · `_release_folder` | `pipeline.py` 809–855 | 파일 `runs/locks/<sha1 16자>.lock` 에 런 id 를 적는다. 쥔 런이 `running` 이 아니면 낡은 락으로 보고 지운다. 큐는 `folder_holder` 만 읽는다 (§4.6) | 읽음 |
| `RelayEngine.disk_usage` | `pipeline.py` 893–920 | `runs/` 바로 아래 폴더를 런으로 센다. `studio` 는 `shadow` · `locks` 와 함께 제외 목록에 넣고(906), DB 합계(`databases_mb`)에 `runs/studio/*.sqlite*` 를 더한다(912–913) — `studio.sqlite` 와 `bak-v<n>` 복사본이다 (§2.2). `runs/campaigns/` 는 지금도 런으로 세어지고 그대로 둔다 | 읽음 (893–920행) |
| `ClaudeCliRunner._emit_activity` · `describe_tool` | `runners.py` 832–866 · 238 | 도구를 부르면 `tool_use`(도구 이름 · `target`) 또는 `mcp_call`, MCP 결과에 `mcp_result`(`chars` · `is_error`)를 이벤트로 남긴다. `Skill` 은 `tool_use` 로만 남고 `target` 은 입력 JSON 200자다. `studio/runlog.py` 가 이 이벤트를 `RunSink.idle` 에서 런별로 모은다 (§10.7 '런별 모음') | 읽음 (832–866행) |
| 런 예산 정지 | `pipeline.py` 1576–1591 | `awaiting_approval` + 이벤트 `budget_exceeded` + `_write_stop(run, "budget", …)`. 승인 방식 `auto` 는 한도의 4배(`AUTO_RUN_COST_FACTOR`, 498)까지 스스로 늘린다 | 읽음 |
| 단계 예산 정지 | `pipeline.py` 1726–1730 | `awaiting_approval` + `pending_budget_stage` | 일부 |
| 설계 게이트 | `pipeline.py` 1776–1790 | 멈추는 조건: 단계가 `gate: human` 이고 뒤 단계가 남았고, 승인 방식이 `always` 이거나 `ai` 이면서 단계 결과가 `needs_approval` 이다. 그때 `awaiting_approval` + 이벤트 `awaiting_approval` + 정지 기록 `awaiting_approval`. 마지막 단계와 `gate` 없는 단계에서는 멈추지 않는다 | 읽음 |
| `RunState` | `pipeline.py` 187–221 | studio 가 읽는 필드: `status`(192) · `error`(197) · `done_sessions`(200) · `pending_budget_stage`(202) · `repo`(203) · `auto_apply`(204) · `stage_models`(205) · `approval`(210) · `model_cap` · `effort_cap`(212–213) · `stage_sessions`(216–217) · `workspace_mode`(218) · `changes` · `changes_status`(219–220) · `workspace_cleaned`(221) · `cost_usd`(223–225) | 읽음 |
| `StopNote` (`run.baton.stop`) | `baton.py` 38–48 · 86 | 런이 끝나기 전에 멈춘 이유. `kind` 는 `cancelled` · `failed` · `awaiting_approval` · `budget` · `stage_budget`(`pipeline.py` 1729. `baton.py` 41 의 주석에는 없다). `stage` · `reason` · `done_stages` · `remaining_stages` 를 가진다. §4.7 의 예산과 설계 게이트 구분에 쓴다 | 읽음 |
| `RelaySpec.workspace` · `max_run_cost_usd` | `pipeline.py` 156 · 158 | 읽기 전용 턴의 판정(`none`), 런 비용 정지 값 | 읽음 |
| `_settle_workspace` | `pipeline.py` 1168–1180 | 런이 멈출 때 변경을 모아 `changes` 를 채우고 `changes_status` 를 `ready`(바뀐 파일 있음) 또는 `none` 으로 둔다. 스냅샷은 지우지 않는다. `changes_status` 가 이미 `applied` 면 모으지 않고 돌아간다(1094) — 반영한 뒤 이은 런의 새 변경은 `changes` 에 들지 않는다 (코드로만 확인. 실행해 보지 않았다) | 읽음 |
| `_fail` | `pipeline.py` 1361–1367 | 런을 `failed` · `cancelled` 로 끝내는 자리. `status`(기본 `failed`, 취소 갈래는 `cancelled`)와 `error`(`[단계] 메시지`)를 적고, 이벤트 `run_failed`(취소면 `run_cancelled`)를 남기고, `_write_stop` → `_settle_workspace` → `save` 뒤 런을 돌려준다. 단계 실행의 `RunnerError`(1738)와 `OSError` · `ValueError`(1739–1740)가 여기로 온다. 그 `except` 밖에서도 부른다 — 예상 밖 오류(1499), 작업 공간 준비 실패(1545 · 1549), MCP 준비 검사(1565), 단계 사이와 단계 실행 중의 취소(1575 · 1707), `verdict=fail`(1763), 재시도 소진(1769). 이렇게 끝난 런을 §4.7 의 표가 받는다 — `failed` 는 `failed` · `working` 행이다 (§10.8 의 9) | 읽음 (본문 1361–1367. 호출 자리는 그 줄만) |
| `_write_stop` | `pipeline.py` 1327–1359 | 정지 기록 `StopNote`(`kind` · `stage` · `reason` 300자 · 끝난 단계 · 남은 단계 · 재개 안내)를 `run.baton.stop` 에 쓰고, AI 없이 만든 인계 요약을 먼저 넣는다(1347). `handoff_writer` 가 있고 쓸 내용(끝난 단계 기록, 그 단계가 한 말이나 고친 파일)이 있으면 런을 저장한 뒤 가벼운 모델로 인계 요약을 다시 쓴다 — 실패하면 이벤트 `handoff_failed` 를 남기고 앞의 요약이 남으며, 성공하면 사용량을 `<단계>:handoff` 로 적고 이벤트 `handoff_written` 을 남긴다. 이 모델 호출은 `_run_stage` 를 지나지 않는다. `handoff_writer` 는 `HandoffWriter`(`handoff.py`)다. 러너를 거치지 않고 `claude -p` 를 직접 띄운다 — `--tools ""` · `--strict-mcp-config` · `--safe-mode` 를 붙이고 임시 폴더에서 돈다. 도구가 없어 가드 훅이 볼 호출이 없다 (S-10 확정 — 코드로 읽었다) | 읽음 (1327–1359행. `handoff.py` 33–49행) |
| 런이 `done` 으로 끝날 때의 반영 · 정리 | `pipeline.py` 1796–1813 | `copy` 이고 `auto_apply` 이고 `changes_status` 가 `ready` 면 그 자리에서 원본에 반영하고 `applied` · `workspace_cleaned = True` 로 둔다(1798–1803). 실패하면 이벤트 `auto_apply_failed` 만 남긴다(1804–1806). `changes_status` 가 `none` 이면 워크스페이스를 바로 정리한다(1807–1812) — T9 조건 1의 예외가 이 때문에 있다. studio 는 `auto_apply = False` 로 만들어 앞 경로를 타지 않는다. 타면 변경이 검수 전에 `applied` 가 돼 T7 · T9 의 조건과 맞지 않는다 (§4.3 T3) | 읽음 |
| `cleanup_workspaces` | `pipeline.py` 857–886, `server.py` 53 · 62 | 시작 때와 1시간마다 돈다. `done` · `failed` · `cancelled` 런만 본다. 변경을 결정한 런(`applied` · `discarded` · `rolled_back`, 또는 `done` 이면서 변경 없음)은 바로, 나머지는 `run.json` 이 보존 기간보다 오래되면 지운다. `awaiting_approval` 런은 지우지 않는다. `review` · `blocked` 인 작업의 런은 기간 만료로 지우지 않는다 — studio 가 거는 `RelayEngine.held_runs` 가 그 런들을 준다 (§4.3 T9. S-12 확정) | 읽음 (857–886행) |
| `Workspace.rollback` | `workspace.py` 319–329 | T9 (`inplace`). 스냅샷이 없으면 `WorkspaceError`. 폴더 전체를 스냅샷으로 맞춘다 | 읽음 |
| `Workspace.discard` | `workspace.py` 311–317 | T9 (`copy`). `inplace` 면 `WorkspaceError`. `apply` 와 마찬가지로 먼저 변경을 다시 모은다(`collect`) — 런이 끝날 때 이미 한 번 모았으므로 두 번째다. 복사본에서 지운 파일이 있으면 그 두 번째 수집이 `git add` 에서 실패해 반영도 버리기도 안 되던 것을 S-12 에서 고쳤다(`_candidates` 가 인덱스에도 디스크에도 없는 경로를 넘기지 않는다. 줄 수는 그대로다) | 읽음 |
| `Workspace.apply` | `workspace.py` 241–260 | T7 (`copy`) 에서 변경 반영. 엔진 쪽 `engine.apply_changes`(`server.py` 842). `inplace` 면 `WorkspaceError`(246–247), 패치가 없거나 비어 있어도 `WorkspaceError`(251–252). 작업 복사본이 남아 있으면 원본이 그사이 바뀌었어도 3방향 병합으로 얹고 겹치는 수정에서만 멈춘다(함수 설명 242–245). 복사본이 정리된 뒤에는 원본이 스냅샷 그대로일 때만 된다(255–257). 반영이 성공하면 곧바로 `cleanup`(259 · 331–347)해 복사본과 marker 를 지우고 `prepared`(153–155)가 False 가 된다 — 그 뒤 커밋이 실패해도 복사본은 없다 (§4.3 T7 의 (나)) | 읽음 (241–260행) |
| `Workspace._merge_onto_source` | `workspace.py` 276–309 | 원본이 그사이 바뀌어 패치가 그대로 적용되지 않을 때만 하는 3방향 병합(`Workspace.apply` 254–258. 원본이 바뀌었어도 패치가 그대로 적용되면 부르지 않는다). `git merge-tree --write-tree` 를 쓴다. git 이 2.38 미만이면 `WorkspaceError`(300–302), 겹치는 수정이면 파일 이름과 함께 거부한다(295–299). 서버 PC 의 git 이 2.38 미만이면 병합이 필요한 `copy` 작업의 통과(T7)는 항상 실패한다 (§4.3 T7 의 (가)) | 읽음 |
| `RelayEngine.apply_changes` · `discard_changes` · `rollback_changes` | `pipeline.py` 1219–1226, `_changes_action` 1182–1217 | T7(`copy` 반영)과 T9. 런이 `done` · `cancelled` 일 때만 받는다(1184–1186) — 그래서 T7 · T9 · T11 이 먼저 `cancel` 한다(§4.3). 워크스페이스가 정리된 뒤에는 `apply` 말고는 `ValueError`(1190–1191). 다른 런이 그 폴더를 원본에서 쓰는 중이면 거부하고(1194–1196), 이미 결정된 변경(`applied` · `discarded` · `rolled_back`)도 거부한다(1197–1198). 동작은 `apply` · `discard` · `rollback` 셋뿐이고(1201–1209) `inplace` 런의 변경을 "그대로 둔다"로 결정하는 동작은 없다. 그래서 S-12 가 그 동작을 더했다 — `RelayEngine.keep_changes`(아래 행). `inplace` 의 T7 은 커밋한 뒤 그것을 불러 `changes_status` 를 `applied` 로 바꾼다. `apply` 가 성공하면 `changes_status = applied`(1127)이고, 어느 동작이든 성공하면 `workspace_cleaned = True`(1136)다. 두 값을 되돌리는 코드는 없어, `copy` 를 반영한 뒤에는 같은 런으로 다시 반영하거나 되돌릴 수 없다 (§4.3 T7 의 (나)) | 읽음 (1182–1217행) |
| 통과 시 git 커밋 | — | `pipeline.py` · `workspace.py` · `server.py` 를 검색했으나 커밋하는 기존 함수를 찾지 못했다. S-12 가 만들었다 — `studio/review.py` 의 `commit(folder, message)`: `git add -A -- .` → 바뀐 것이 있으면 `git commit -- .`, 푸시 없음, 저장소가 아니면 건너뛴다. 커밋이 실패하면 인덱스를 `add` 전으로 되돌린다 (`write-tree` → `read-tree`). 부르는 자리는 §4.3 T7 — `copy` 는 `apply_changes` 뒤(이미 `applied` 면 반영을 건너뛰고), `inplace` 는 엔진 호출 없이. 커밋할 것이 없으면 성공으로 본다 (§4.5) | 일부 (검색만) |
| `ready` 로 남은 런을 읽는 곳 | `pipeline.py` 714–725 · 950–962 | `delete_check` 는 세션의 런 가운데 `changes_status` 가 `ready` 인 것을 모아(719) `copy` 면 `unapplied` 로 내놓는다(724). shadow 저장소 목록은 `ready` 런을 `undecided` 로 내놓는다(958). `inplace` 로 통과(T7)한 작업의 런이 `ready` 로 남으면 이 둘에 미결정으로 잡히고 스냅샷도 보존 기간이 지나야 지워진다. 그래서 통과 때 커밋한 뒤 `keep_changes` 가 `applied` 로 바꾸고 스냅샷을 정리한다 (S-12 확정) | 읽음 (714–725 · 950–962행) |
| `RelayEngine.reopen` · `keep_changes` | `pipeline.py` 클래스 끝 (`_let_go` 뒤) | S-12 가 더한 두 함수. 이 문서가 인용한 줄 번호가 밀리지 않게 클래스 끝에 둔다. `reopen(run_id)`: `done` 런을 `pending` 으로 돌려 끝낸 대화로 잇게 한다 (§4.7 의 `done` 행). `keep_changes(run_id)`: `done` · `cancelled` 인 `inplace` 런의 `ready` 변경을 "그대로 둔다"로 결정한다 — `changes_status = applied`, 스냅샷 정리, 폴더는 건드리지 않는다. 조건이 안 맞으면 둘 다 `ValueError` | 읽음 |
| `recover` | `server.py` 49–66 | `engine.recover_interrupted()` → `campaigns.recover()` → `engine.cleanup_workspaces(…)`. studio 복구를 이 뒤에 넣는다 (§4.8). 작업 없는 런의 결재 채우기(§5.8 나의 "시작할 때")는 이 함수 안이 아니라 `server.py` 맨 아래의 startup 함수 `file_waiting_runs` 가 한다 — 등록 순서대로 돌아 이 함수 뒤다 | 읽음 |
| `RelayEngine.recover_interrupted` | `pipeline.py` 1016–1029 | 서버가 멈출 때 `running` 이던 런(다른 살아 있는 프로세스가 쥔 것은 제외)을 `failed` 로 바꾸고 오류를 "서버 재시작으로 중단됨 …" 으로 적는다. 다시 잇지는 않는다 | 읽음 |
| 서버 상시 가동 (V-3) | `server.py` startup · shutdown 맨 끝 | `studio/awake.py` 의 `KEEPER.enable()` · `disable()` 과 `studio/disk.py` 의 `DiskWatch.watch()` · `unwatch()` 만 연결한다. `TaskQueue` · `ChatTurns` 의 생성자 줄은 고치지 않는다 — 둘은 기본으로 `KEEPER` 를 쓴다 (§4.8 절전 방지). `supervise.py` · 전원 설정 · 시작 프로그램은 고치지 않았다 | 읽음 |
| 사용량 안전망 (V-4) | `server.py` 큐 생성 줄 · V-3 블록 아래 | `TaskQueue(hold=, on_held=)` 두 인자를 넘기고 `studio/usage.py` 의 `UsageWatch.watch()` · `unwatch()` 를 startup · shutdown 에 연결한다. `providers.py` · 엔진 · `switch_at_utilization` 은 고치지 않았다 | 읽음 |
| `/runs/{id}/approve` · `resume` · `cancel` | `server.py` 814–836 | studio 밖에서 런을 조작하는 기존 통로. `resume` 은 `pending` · `failed` · `cancelled` 만 받는다(825) | 읽음 |
| `CampaignRunner._run_task` | `campaign.py` 380–415 | S-14 는 여기를 고치지 않았다 — 확장 지점은 클래스 끝의 `studio` · `advance_studio` 다 (§4.10). 기존 캠페인은 `engine.create(…, needs_writes=True)` 로 런을 만들고 `_drive_run` 으로 끝까지 민 뒤 `done` 이면 작업을 끝낸다 | 읽음 |
| `CampaignRunner._drive_run` | `campaign.py` 431–465 | 런을 끝 상태까지 민다. `failed` 의 오류가 "서버 재시작"으로 시작하면 바로 재개, "다른 실행(" 이 있으면 기다렸다 재개, 사용량 한도면 풀릴 때까지 기다렸다 재개, 그 밖에는 `max_attempts` 까지 다시 한다. studio 의 작업 런도 앞의 셋은 같은 방식으로 다시 잇는다 (§4.7). 런을 미는 걸음은 `_settle`(436 에서 호출, 아래 행)이다 | 일부 |
| `CampaignRunner._settle` | `campaign.py` 467–492 | `engine.advance` 를 부른 뒤 런이 끝 상태(`TERMINAL`, `campaign.py` 31 — `done` · `failed` · `cancelled`)가 될 때까지 `POLL_S` 간격으로 본다. `awaiting_approval` 이면 캠페인을 `waiting` 으로 두고 사유에 "대시보드에서 승인하면 이어서 진행"을 적은 채 기다린다(480–485). 누가 승인해 런이 `pending` 이 됐는데 아무 스레드도 쥐지 않았으면 스스로 `advance` 한다(486–491). 정지 요청이 있으면 캠페인을 멈추고 `None` 을 돌려준다(477–479). studio 는 이렇게 기다리지 않는다 — 진행 스레드가 돌아와 T4 를 하고 슬롯을 놓는다 (§4.7) | 읽음 |
| `CampaignRunner._followup` | `campaign.py` 417–423 | 남은 이슈를 닫는 재시도 작업(`max_followups`). S-12 는 이것을 같은 작업의 재시도로 두고, 범위가 늘어나는 것만 `followup_propose` 로 올린다 — 부모(`parent_id`)가 연결된 `proposed` 작업이 되어 owner 의 승인을 거친다 (§8.4. S-12 확정) | 일부 |
| `CampaignSpec` | `pipeline.py` 140–150 | `task_relay` · `max_tasks` · `max_followups` · `max_cost_usd` · `max_attempts` · `model_cap` · `effort_cap` | 읽음 |

T3 에서 `RelayEngine.create` 에 넘기는 값:

| 인자 | 값 |
|---|---|
| `relay_path` | `tasks.relay` → 없으면 `employee.yaml` 의 릴레이 → 없으면 `departments.default_relay` 의 파일 경로 (§9.5 의 1). 그 릴레이가 `auto` 면 공개 라우팅 메서드가 고른 릴레이의 경로를 넘긴다 — `auto.yaml` 의 경로는 넘기지 않는다 (§9.5) |
| `goal` | 작업의 제목 · 사유 · 명세 · 완료 기준 + 직원 지침 · 기억 + 프로젝트 정책 (조립 방식은 S-8) |
| `session_id` | 작업방의 그 직원 행 `room_members.session_id` (S-8 확정). 그 세션이 history 에 있고 같은 저장소의 것일 때만 넘기고, 아니면 엔진이 새 세션을 연다. T3 를 커밋한 뒤 그 런의 세션을 이 행에 적는다 (`WorkEffects.after` — 행은 T3 의 트랜잭션이 만든다) |
| `repo` | `projects.repo`. 경로는 저장소 설정을 따른다. 워크스페이스 방식은 저장소 설정에서 출발하지만(556) 엔진이 바꿀 수 있어 — 파일을 고치는 단계가 없으면 `none`(554–555), 읽기 전용이 아닌 MCP 가 붙으면 `copy` 가 `inplace`(584–586) — studio 는 만들어진 런의 `RunState.workspace_mode` 를 읽어 쓴다 (§4.3 T3 · T7). `auto_apply` 는 저장소 설정을 따르지 않는다 (아래 행) |
| `auto_apply` | `False` 고정. 저장소 · 릴레이 설정을 따르지 않는다 (§4.3 T3. S-8 확정) `[§12]` |
| `stage_models` | `employee.yaml` 의 `work` 값에 `tasks.stage_models_json` 을 덮어쓴 것. 단계에 펼치고 상한을 건 뒤의 칸이다 (§9.5 의 2–6) |
| `approval` | `ai` (§4.7) |
| `mcp` | `employee.yaml` 의 `mcp` 에 `studio` 를 더한 것 (§9.2, §9.5 의 7). `studio` 때문에 `copy` 가 원본 작업으로 바뀌지 않게 하는 자리는 위 `_run_mcp_names` 행이다 |
| `pin_copy` — `copy` 고정 (S-8 확정) | 서버 코드를 품은 프로젝트의 작업 런에만 준다 (§4.3 T3). 방식이 `inplace` 로 정해졌거나(556) 584–586 에서 바뀔 참이면 엔진이 런을 만들지 않고 `ValueError` 를 낸다. `none` 은 파일을 고치는 단계가 하나도 없을 때만 통과한다(554–555). 고치는 단계가 있는데 `none` 이면 `inplace` 와 똑같이 `ValueError` 로 거부한다 — `none` 런은 `Workspace` 없이 원본 폴더에서 돈다(382–384 · 1551). 그 밖의 프로젝트에는 주지 않는다 — 지금처럼 엔진이 바꾸고 `workspace_forced` 를 남긴다 |
| `model_cap` · `effort_cap` | 상한. `employee.yaml` 의 `cap` → 파트 기본값 → 설정 순으로 찾는다 (§9.5 의 6). `stage_models` 로 넘긴 값에는 엔진이 상한을 걸지 않으므로 studio 가 먼저 검사한다 |
| `needs_writes` | `True`. 릴레이가 `auto` 일 때 공개 라우팅 메서드에 넘기는 값이다 — 파일을 고치는 단계가 없는 릴레이를 후보에서 뺀다 (`router.py` 49–50). `create` 는 읽은 범위(536–654행)에서 이 값을 `_route` 에만 넘기므로(546–547), 고른 경로로 부를 때는 영향이 없다 |
| `routed` — 라우팅 결과 (S-8 확정) | 릴레이가 `auto` 였을 때 공개 라우팅 메서드가 돌려준 `{relay, reason, by, usage, from}`. `create` 가 이벤트 `relay_routed` 와 라우터 사용량(`-:router`)을 남긴다 (620–625 와 같은 기록). `auto` 가 아니면 주지 않는다 |

채팅 · 읽기 턴의 `create` 는 `relay_path` 가 `relays/employee-chat.yaml`, `stage_models` 가 `chat` 칸 하나, `mcp` 가 `chat_mcp` + `studio` 이고, 여기에 MCP 한정 인자 `mcp_only`(값은 `mcp` 와 같다. S-7 확정)와 작업 폴더(`repo` 또는 `workdir`), `approval` `ai`, 그 방의 `room_members.session_id` 를 더한다. 대화를 잇는 턴은 `create` 뒤에 `RunState.stage_sessions['chat']` 에 `room_members.conversation_id` 를 넣고 새 글을 `interject` 로 전한다 — 엔진이 그 대화를 `--resume` 하고 새 글만 보낸다 (`studio/chat.py`) (§9.5 의 덧붙임).

런에 직원 ID 를 주입하는 방법(studio MCP 가 호출자를 식별하는 근거)은 §8.2 다. 기존 코드에서 찾은 자리는 `_stage_mcp`(`pipeline.py` 1406–1414, 읽음)다. `digest` 서버의 `env` 에만 `KATAE_WORKDIR` · `KATAE_RUN_ID` · `KATAE_STAGE` · `KATAE_EXTRA_DIRS` · `KATAE_USAGE_DB` 를 넣고, 레지스트리에 `digest` 가 없으면 아무것도 하지 않는다. studio MCP 서버에 직원 ID 를 넣으려면 이 함수를 넓혀야 한다. S-6 이 그 옆에 `_env_mcp` 와 `RelayEngine.mcp_env` 를 더했다 (§8.2) — `_stage_mcp` 자신과 `digest` 의 값은 그대로다.

### 11.3 API · 스트림

서버 · 알림과, §8 이 기대는 MCP 쪽 자리다. 훅과 권한 프로파일은 §11.4 가 더한다.

`studio/harness_setup.py` (T3): `jobs.Jobs` 의 `on_registered` 훅과 가져오기 라우트가 `api._bring_in` 을 부르고, 그것이 `harness_setup.auto` → `note` → `_begin_policy_talk` 순으로 돈다. HR 방을 고르는 일은 `policy.hr_room` 이다(`begin_interview` 와 같다). 모두 예외를 삼킨다(§6.4).

| 기존 코드 | 위치 | studio 가 쓰는 방식 | 확인 |
|---|---|---|---|
| `guard` 미들웨어 | `server.py` 90–113 | 출처 검사(96–98) · 네트워크 검사(99–101)는 `/studio/*` 도 그대로 탄다. 토큰 검사(107–112)는 경로가 `/studio/` 로 시작하면 studio 인증으로 갈린다 (§6.2). 이 갈래는 루프백 통과(107)보다 앞에 둔다 — `/studio/*` 는 루프백에도 토큰을 요구한다. 그 밖의 경로는 지금 그대로다: 루프백은 토큰 없이 통과하고(107), 공용 토큰이 없으면 원격은 403(109–110), 틀리면 401(111–112)이다. `/` 는 토큰 검사를 하지 않는다(107) | 읽음 |
| `guard` 와 WebSocket | `server.py` 90 | `@app.middleware("http")` 라 WS 연결을 거치지 않는다. `/studio/ws` 가 인증을 직접 한다 (§7.1). 네트워크 검사는 가드와 같은 함수(`_network_allowed`)를 쓴다 | 테스트로 확인 (S-4, `tests/test_studio_ws.py`) |
| `_conflict` | `server.py` 214–218 | 엔진의 `ValueError` 를 409 `{detail}` 로 바꾼다. studio 는 같은 경우를 `engine_error` 로 내고(§6.3), 작업 런을 막는 409 도 이 형식이다 (§5.8 다) | 읽음 |
| `POST /attachments` · `_attachment_paths` | `server.py` 151–165 · 168–176 | base64 JSON 업로드, 파일당 25MB(144), 이름 정리(161), 폴더 밖 경로 거부(172–174). `/studio/attachments` 가 같은 방식을 따른다 (§6.6) | 읽음 |
| `POST /runs/{id}/interject` | `server.py` 293–296 | owner 가 작업방에 쓴 글의 끼어들기와 같은 호출 (§6.6). 작업 런에도 그대로 받는다 (§5.8 다) | 읽음 |
| `GET /models` · `ProviderRegistry.catalog` | `server.py` 299–302, `providers.py` 276–295 | `/studio/models` 가 Claude Code 의 항목만 골라 돌려준다 (§6.4, §10.8 의 9). mock 과 쓸 수 없는 AI 는 빠지고(280–284), 모델마다 `tier` · `exhausted_until` · `efforts` 가 있다 | 읽음 |
| `GET /providers` | `server.py` 274–275 | `GET /studio/machine` 의 `providers` (M-1). 응답에서 본 필드는 `katae.py` 47–52 가 읽는 `name` · `available` · `reason` · `limits[{window, utilization}]` · `usage_24h.cost_usd` 뿐이다 | 일부 |
| `GET /runs/{id}/live` · `RelayEngine.liveness` | `server.py` 727–730, `pipeline.py` 743–778 | `run.live` 와 `/studio/runs/{id}/live` 의 내용 (§7.4). 다른 프로세스가 쥔 런은 디스크의 `pulse.json` 을 읽는다(752–756) | 읽음 |
| `GET /notifications` | `server.py` 733–745 | `after` 커서로 `NOTIFY_KINDS` 이벤트를 읽는 기존 폴링. `after=-1` 은 지금의 커서만 돌려준다. studio 는 쓰지 않는다 — 이어 받기는 `stream_events` 다 (§7.2). `after_id` 생략 = 지금부터(§7.1)가 같은 관례다 | 읽음 |
| `/runs/{id}/approve` · `resume` · `cancel` | `server.py` 814–836 | 작업 런이면 `approve` · `resume` 은 409, `cancel` 은 받는다. 작업 없는 런이면 그 런의 `pending` 결재를 닫는 연결 지점을 `approve` · `cancel` 에 둔다 (§5.8) | 읽음 |
| `/runs/{id}/changes/apply` · `discard` · `rollback` | `server.py` 839–854 | 작업 런이면 409 (§5.8 다) | 읽음 |
| `RelayEngine.approve` 의 예산 처리 | `pipeline.py` 656–671 | 런 예산이면 `budget_extra_usd = cost_usd`(661–663), 단계 예산이면 `stage_budget_boost` 를 2배(664–667). `budget` 결재의 두 결정이 모두 이 길로 잇는 근거 (§5.5) | 읽음 |
| 예산 · 게이트의 정지 기록 | `pipeline.py` 1583–1591 · 1726–1732 · 1777–1790 | `StopNote.kind` 가 `budget` · `stage_budget` · `awaiting_approval` 이다. `stage_budget`(1729)은 `baton.py` 41 의 주석에 없는 값이다. §4.7 과 위 §11.2 의 `StopNote` 행은 이 값을 넣어 다섯으로 적는다 (§5.8 가) | 읽음 |
| `GET /runs/{id}/patch` · `/tokens` · `/usage` | `server.py` 808 · 748 · 888 | `/studio` 아래의 별칭이 그대로 돌려준다 (§6.4). 응답 모양은 읽지 않았다 | 일부 (경로만) |
| `Notifier` · `NOTIFY_KINDS` | `notify.py` 45–67 · 16–24 | 호출 형태는 `notifier(run_id, kind, title, detail)` 이고 Windows 토스트만 띄운다. 종류는 `run_done` · `run_failed` · `awaiting_approval` · `stage_budget_exceeded` · `budget_exceeded` · `campaign_done` · `campaign_paused`. N-1 은 여기에 FCM 채널을 더했고(`studio/push.py` 의 `attach` — `Fanout` 의 수신기 하나. `server.py` 끝에서 건다), studio 는 같은 자리에서 작업 없는 런의 결재를 만든다 (§5.8 나, §5.9). 엔진은 `RelayEngine._event`(`pipeline.py` 374–380)에서 `NOTIFY_KINDS` 에 든 종류만 넘기고 예외는 삼킨다. 수신기를 여럿 거는 것은 `Fanout`(`notify.py`, S-5 가 더했다)이다 | 읽음 |
| `mcp_servers/katae.py` | 18–41 · 44 | studio MCP 서버의 형태: `sys.path` 에 저장소를 넣고(18), `Config.load()` 로 주소와 토큰을 얻고(26–28), `FastMCP` 와 `@mcp.tool()` 을 쓴다. `_call` 은 `httpx.request` 에 Bearer 헤더를 붙이고 오류를 `RuntimeError` 로 올린다(32–41) | 읽음 |
| `RelayEngine._stage_mcp` | `pipeline.py` 1406–1414 | 행위자 토큰을 넣을 자리 (§8.2). 지금은 `digest` 서버의 `env` 만 채우고 `digest` 가 없으면 아무것도 하지 않는다 | 읽음 |
| 러너의 MCP 설정 파일 | `runners.py` 664–674 | 단계마다 임시 폴더(`katae-` 접두사)에 `mcp.json` 을 쓰고 단계가 끝나면 폴더째 지운다. 행위자 토큰이 그동안 이 파일에 있다 (§8.2). 레지스트리에 없는 서버 이름이면 `RunnerError` 다(668–670) | 읽음 |
| MCP 레지스트리 | `server.py` 614 (`GET /mcp`), `config.py` 24–25 · 80–84 | `studio` 서버를 레지스트리에 올리는 자리. 레지스트리는 내장 항목에 `Config.mcp_servers` 를 합친 것이다 (§11.4) | 일부 (`config.py` 의 해당 줄만) |

### 11.4 MCP · 훅

studio MCP 서버(§8)와 가드 훅(§10)이 기대는 자리다. "studio 런"은 작업 런과 채팅 · 읽기 턴의 런 — `tasks.run_id` 나 `chat.py` 의 런 장부에 있는 런 — 이다 (§8.2).

| 기존 코드 | 위치 | studio 가 쓰는 방식 | 확인 |
|---|---|---|---|
| `mcp_servers/` | `katae.py` · `handoff.py` · `digest.py` · `docs_read.py` | `studio.py` 를 같은 폴더에 둔다. 형태는 `katae.py` 를 따른다 (§8.1, §11.3) | 일부 (파일 목록만. `katae.py` 는 §11.3) |
| MCP 레지스트리 | `config.py` 24–25 · 80–84 | 이름 → Claude Code `mcpServers` 항목이다. 내장 항목(`docs_read` · `handoff` · `digest`, 각 `{command, args}`)에 `Config.mcp_servers` 를 합친다(84). `studio` 를 내장 항목에 더했다 (S-6: `{command, args: [mcp_servers/studio.py]}`, `env` 없음). `env` 는 단계마다 `RelayEngine.mcp_env` 의 콜백이 채운다 (§8.2) — 콜백이 `None` 을 주는 런(대시보드 · CLI 런)에는 고르거나 단계에 적어도 붙지 않는다. `employee.yaml` 의 `mcp` 검사(§9.4 의 6)가 이 목록을 본다. 러너가 이것을 `mcp_registry` 로 받는 줄은 읽지 않았다 | 일부 (해당 줄만) |
| `backup_hook.py` | 1–60 | PreToolUse 훅의 기존 형태다. argv 로 `<run_dir> <project_root>` 를, stdin 으로 도구 호출 JSON 을 받고 `tool_input.file_path` · `notebook_path` 를 읽는다(47–53). 어떤 실패에도 exit 0 이다(10 · 54–56). 가드는 같은 방식으로 입력을 받되 거부한다 (§10.6) | 읽음 |
| `JournalWorkspace.hook_settings` | `journal.py` 285–292 | 훅 설정 파일을 쓴다. `hooks.PreToolUse` 에 항목 하나 — `matcher` 는 편집 도구 넷, `hooks` 는 `{type: "command", command}` 이고 명령 줄은 파이썬 · `backup_hook.py` · 런 폴더 · 원본 폴더다. 명령 줄에는 argv 만 있다 | 읽음 |
| 설정 파일이 단계로 가는 줄 | `pipeline.py` 1552 · 1664, `runners.py` 594–595 | 워크스페이스가 `JournalWorkspace` 이고 설정 파일이 있을 때만 `StageCall.settings_path` 가 차고(1552) 러너가 `--settings` 로 넘긴다. studio 런이면 워크스페이스 종류와 무관하게 넘기도록 넓힌다 | 읽음 |
| `--safe-mode` | `runners.py` 591–593 | `isolate` 이고 `project` 가 아닌 단계는 MCP 도 설정 파일도 없으면 `--safe-mode` 가 붙는다. 주석대로면 MCP 와 훅이 꺼진다. studio 런은 둘 다 가지므로 붙지 않는다 (§10.8 의 10). 게임 프로젝트 런과 작업 런은 엔진이 `StageCall.isolate` 를 꺼서 러너를 고치지 않고도 플래그가 빠진다 (§10.1) | 읽음 |
| `StageSpec.tools` · `allowed_tools` · `permission_mode` | `pipeline.py` 96–99 | 릴레이 파일이 단계마다 정하는 권한이다. studio 는 고치지 않는다 (§10.1) | 읽음 |
| `allowed_tools` 조립 | `pipeline.py` 1657–1659 | 단계의 `allowed_tools` 에, `Bash` 가 있으면 `extra_allowed_tools` · 저장소의 verify 명령 · 프로젝트의 것을 더한다. `studio-full` 의 묶음을 여기에 더한다 (§10.1). 게임 프로젝트면 `Skill` 과 인덱스 명령(`projctx.index_allowed`)도 더하고, `_stage_tools` 는 `Skill` 을 붙이고 `Bash` 를 지키며 `isolate` 를 끈다 | 읽음 |
| `--tools` · `--allowedTools` | `runners.py` 604–610 | 쓸 수 있는 도구와 사전 승인을 따로 넘긴다. 붙은 MCP 서버는 `mcp__<이름>` 으로 사전 승인에 들어간다(606). `Bash` 가 있으면 읽기 전용 명령(`READ_ONLY_BASH`, 540)이 더해진다(607–608). `--permission-mode` 를 넘기는 줄은 읽지 않았다 | 일부 |
| `Config.extra_allowed_tools` | `config.py` 42–44 | `Bash` 가 있는 모든 단계에 더하는 사전 승인이다. `studio-full` 묶음과 같은 형태의 선례다 | 읽음 |
| `StageCall.env` | `runners.py` 78 · 268, `pipeline.py` 1663 | CLI 프로세스에 더하는 환경 변수다. 지금은 `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` 하나만 넣는다(1663). 값이 있으면 `{**os.environ, **call.env}` 가 프로세스 환경이 된다(268). 가드 훅에 `STUDIO_URL` · `STUDIO_ACTOR_TOKEN` 을 넘길 자리다. 훅 프로세스가 CLI 의 환경을 물려받는지는 확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "훅 프로세스가 CLI 의 환경 변수(`STUDIO_URL` · `STUDIO_ACTOR_TOKEN`)를 물려받는지") | 읽음 (해당 줄) |
| `RelayEngine._run_stage` | `pipeline.py` 1421–1443 | 단계의 후보를 만든다 — 쓰일 provider, 단계의 `alternates`(1424), 칸이 provider 를 바꿨으면 릴레이 자신의 AI(1425–1426). `providers.candidates` 로 쓸 수 있는 것만 남기고(1431), 하나도 없으면 `RunnerError`(`unavailable`, 1433–1434)다. 있으면 차례로 시도하고 넘어갈 때 이벤트 `provider_switch` 를 남긴다(1440–1441). `is_claude`(1442–1443)는 `anthropic_api` · `mock` · `kind` 가 `api` 인 것도 참이다. studio 런이면 여기서 Claude Code 가 아닌 후보를 뺀다 (§10.8 의 9, 아래 6) | 읽음 (1421–1448행) |
| `ProviderRegistry.candidates` | `providers.py` 184–200 | `(primary, alternates, needs_tools)` 를 받아 쓰일 것 → 대체 → `fallback_chain`(186) 순으로 훑고, 쓸 수 있는 것과 건너뛴 것(이유 포함)을 돌려준다. `fallback_chain` 을 빼는 인자가 없다. 시그니처는 그대로 둔다 | 읽음 |

**S-10 이 더하는 연결 지점** (S-10 확정: 콜백은 `RelayEngine.guard_stage` 이고 `server.py` 가 `studio/api.py` 의 `guard_stage` 를 등록한다. 표시 필드는 `StageCall.claude_only` 다. 판정 재료와 설정 파일을 쓰는 코드, `POST /studio/guard/check` 의 분기는 `studio/guard.py` 에 있다):

1. 엔진에 콜백 하나를 등록한다 — "런 id 와 단계를 주면, studio 런일 때 `{allowed_tools, settings_path, env}` 를 돌려준다". `StageCall` 을 조립하는 자리(`pipeline.py` 1645–1670)가 부르고 돌려받은 것을 기존 값에 더한다. §8.2 의 `_stage_mcp` 콜백과 같은 방식이고, 엔진은 직원을 모르는 채로 남는다.
2. 훅 설정 파일은 런에 하나다: `runs/<run_id>/studio_settings.json`. `hooks.PreToolUse` 에 가드 항목(모든 도구에 걸리는 `matcher`, 명령 줄은 파이썬 · `studio_guard.py` · `guard_ctx.json` 의 경로)을 넣고, `JournalWorkspace` 런이면 `hook_settings` 가 쓰던 백업 훅 항목을 함께 넣는다. S-10 확정: 그 파일(`runs/<run_id>/hook-settings.json`)에서 옮기는 것은 `hooks.PreToolUse` 항목뿐이다 — 다른 최상위 키(`disableAllHooks` · `permissions` 등)와 다른 훅 이벤트는 옮기지 않는다. 그 파일도 `protected[]` 에 든다 (5): 거기 적은 것이 다음 단계의 훅 설정으로 옮겨지기 때문이다. 백업 훅이 없는 런에도 그 경로를 넣는다. 모든 도구에 거는 `matcher` 의 표기, 한 도구에 훅이 둘일 때의 실행 순서, 하나가 거부했을 때 나머지가 도는지는 확인 필요 (사람 — `VERIFICATION.md` 의 S-10 "`matcher` 가 `*` 인 훅이 모든 도구에 걸리는지, 훅 둘(가드 · 백업)이 함께 도는지"). S-10 확정: `matcher` 는 `*` 로 적는다 (`guard.py` 의 `MATCH_ALL`).
3. `runs/<run_id>/guard_ctx.json` 은 런을 만들 때 쓴다 (§10.2). 비밀이 아닌 판정 재료만 넣는다. S-10 확정: 런을 만들 때가 아니라 1 의 콜백이 단계마다 다시 쓴다 — 훅 설정 파일도 같다. 도중에 바뀌었어도 다음 단계에서 서버의 것으로 돌아온다. 콜백이 예외를 내면 그 단계는 가드 없이 돌지 않고 실패한다 (`mcp_env` 콜백과 반대 방향).
4. 토큰과 서버 주소는 `StageCall.env` 로 넘긴다. 설정 파일의 명령 줄에 적지 않는 이유: 토큰은 단계마다 새로 만드는데(§8.2) 설정 파일은 런에 하나이고, 명령 줄에 적으면 토큰이 디스크에 남는다. 훅과 studio MCP 서버가 같은 토큰을 쓴다.
5. `guard_ctx.json` 과 훅 설정 파일은 `protected[]` 에 든다. 서버 코드(서버 루트의 `relay_agent/` 전체)도 든다 — 이 절의 연결 지점이 거기 있다 (§10.8).
6. 실행 중의 대체를 Claude Code 로 묶는다 (§10.8 의 9). 1 의 콜백이 돌려주는 것에 표시 하나를 더해 `StageCall` 에 싣고(S-10 확정 — `StageCall.claude_only`), 표시가 있으면 `_run_stage` 가 `candidates` 의 결과에서 `kind` 가 `claude_cli` 가 아닌 후보를 뺀다 (`RelayEngine._hookable`. 테스트용 `mock` 은 남긴다). 뺀 후보는 건너뛴 목록에 이유와 함께 넣어 `unavailable` 의 메시지에 보이게 한다. `candidates` 의 시그니처와 `fallback_chain` 설정은 건드리지 않는다 — studio 밖의 런은 지금처럼 넘어간다.

---

## 12. 캠페인 문서와 어긋나는 점

본문에서 §1.1 의 표기로 태그한 53곳을 절 묶음별로 모았다. 한 행이 여러 자리를 받으면 "태그 자리"에 모두 적었다. 어긋남은 CAMPAIGN.md 에 없는 것을 더했거나 다르게 정한 것이다. CAMPAIGN.md 는 고치지 않았다 — owner 가 이 문서를 승인하면(P-5 의 완료 기준) 아래 표가 두 문서의 차이를 설명한다.

### 12.1 데이터 · 식별자 (§1–§3)

| # | 태그 자리 | CAMPAIGN.md | 이 문서 |
|---|---|---|---|
| 1 | §1.5 | S-6: `ask_owner` 의 `kind` 는 `question` · `choice` · `approval` | 결재 종류 이름은 `question` · `structure_choice` · `pre_approval` 이다. 도구 인자는 S-6 그대로 두고 서버가 바꾼다 |
| 2 | §3.1 | S-1: 저장소 클래스는 `HistoryStore` 와 같은 형태 | 형태는 같고 넷이 다르다 — 외래 키 켜기, `BEGIN IMMEDIATE` 를 직접 쓰기, `transaction()` 컨텍스트, 스키마 버전 |
| 3 | §3.1 · §6.1 | 시각 형식을 적지 않는다. 컴퓨터는 여러 대다 | 서버의 지역 시각이고 시간대 표시가 없다. 앱은 `GET /studio/machine` 의 `now` · `utc_offset_minutes` 로 바꾼다 |
| 4 | §3.3 | 프로젝트는 "릴레이 repo 이름"을 가진다 | 프로젝트 하나에 repo 하나, repo 하나에 프로젝트 하나로 묶었다 |
| 5 | §3.5 | 직원은 "프로젝트에 배치" | 한 번에 한 프로젝트에만 배치된다. 배치용 조인 테이블이 없다 |
| 6 | §3.5 | 직원 상태 넷, H-3 의 퇴사(보관) | 퇴사는 상태 값이 아니라 `retired_at` 이다. 퇴사한 직원의 이름은 다시 쓸 수 없다 |
| 7 | §3.6 | 방은 1:1 · 작업방 · 파트방 · 시스템 공지 | 개수를 정했다 — 직원마다 1:1 방 하나, 작업마다 작업방 하나, 파트마다 파트방 하나, 시스템 방은 전체에 하나 |
| 8 | §3.6 | 작업방은 "나 + 배치 인원" | owner 는 `room_members` 에 행이 없다. 읽은 위치는 `owner_read_id` 에 둔다 |
| 9 | §3.7 | 전제 1: 직원×방마다 Claude Code 대화 하나 | 릴레이는 단계마다 대화가 따로다. 작업 런은 엔진의 단계별 세션(`stage_sessions` · `done_sessions`)을 쓰므로 글자 그대로 "하나"가 아니다 |
| 10 | §3.10 | 담당을 언제 바꿀 수 있는지 적지 않는다 | `backlog` 에서만 바꾼다 |
| 11 | §3.13 | 작업 하나가 결재를 몇 개 가질 수 있는지 적지 않는다 | `pending` 결재는 작업에 하나뿐이다 |
| 12 | §3.13 | `inbox` 의 컬럼을 적지 않는다 | `delivery` · `delivered_at` 을 더했다. 결정문을 런에 넣었는지 기억한다 |
| 13 | §3.14 | M-2: 1회용 페어링 코드 (5분 만료) | 테이블에 두지 않고 서버 메모리에 둔다. 재시작하면 코드는 무효다 (M-2 가 확정했다) |
| 14 | §3.16 · §7.2 (두 곳) | S-1: 테이블 12개. S-4: `after_id` 로 이어 받기 | 13번째 테이블 `stream_events` 를 마이그레이션 버전 2 로 둔다. 오래된 행을 지우므로 "행을 지우는 테이블은 `task_deps` 뿐"에 예외가 하나 생긴다 |

### 12.2 상태 전이 · 큐 (§4)

| # | 태그 자리 | CAMPAIGN.md | 이 문서 |
|---|---|---|---|
| 15 | §4.2 | §2 의 그림에 되돌리기 뒤의 상태가 없다. S-12 는 되돌리기를 결정의 한 종류로만 적는다 | T9 `review` → `backlog` 를 더했다 |
| 16 | §4.3 T3 | M-7: MCP 작업 시작 전 에디터 확인, 무응답이면 결재 | 에디터 검사는 전이 조건이 아니라 전이 뒤의 걸음이다. 무응답이면 `working` 이 된 직후 `blocked` 가 된다. 검사는 `work.py` 의 `WorkEffects.editor_ready`, 런 도중의 감시는 `editor_watch.py` 다 |
| 17 | §4.3 T3 · §11.2 | 저장소 · 릴레이 설정의 `auto_apply` 를 다루지 않는다 | 작업 런은 항상 `auto_apply = False` 로 만든다. 검수 전에 변경이 원본에 반영되지 않게 한다 (S-8 확정) |
| 18 | §4.3 T3 | 상태는 일곱이고 "줄 선 중"이 없다 | 시작 요청과 전이를 나눴다. 전이는 슬롯을 얻은 때 일어나고 그 사이는 `queued_at` 컬럼이다 |
| 19 | §4.3 T4 | `blocked` 로 가는 계기는 질문 · 구조 선택 · 사전 승인 · 위험 명령 넷 | 여섯을 더했다 — 예산 한도, 설계 게이트, 런의 실패 · 취소, 검수 요청 없는 종료, 왕복 한도, 에디터 무응답 (§4.3 T4 의 표). 런이 실패한 작업을 `working` 에 두면 아무도 알림을 받지 못한다 |
| 20 | §4.5 · §6.4 | 작업의 취소 · 중지를 적지 않는다 | 종결 상태 `cancelled` 와 취소 전이 T10 · T11 을 더했다 — owner 만, `backlog` · `blocked` 에서만 (owner 확정, §12.6 의 13). `rejected` 는 제안 거절 전용이고, `stop` 은 전이가 아니며 그 끝은 `blocked` 다 |
| 21 | §4.6 | S-8: 읽기 전용 턴은 큐를 타지 않는다 | 채팅 턴도 큐를 타지 않고, 둘 다 두 한도에 세지 않는다 (S-8 확정) |
| 22 | §4.6 | 진행 방식은 캠페인이 가진다 | 캠페인 없는 작업은 `each` 와 같게 다룬다 (S-14 확정) |

### 12.3 결재 (§5)

| # | 태그 자리 | CAMPAIGN.md | 이 문서 |
|---|---|---|---|
| 23 | §5.2 | A-9: 버튼은 "승인 / 반려 / 수정 요청" | 종류마다 결정 값이 다르다 (§5.2 의 대응표) |
| 24 | §5.3 | H-6: 인사가 정책 · 지침 수정안을 결재로 올린다 | H-6 용 종류를 따로 두지 않고 `hire` 의 `action`(`hire` · `guide_amend` · `policy`)으로 받는다 |
| 25 | §5.3 | M-7 "무응답이면 결재", S-9 "한도 초과 시 `ask_owner` 결재" | 종류를 따로 두지 않고 `question` 의 `reason` 으로 받는다 |
| 26 | §5.4 | S-12: 검수 요청 시 패키지를 "생성"한다 | 직원이 낸 것만 저장하고 나머지는 `GET /studio/inbox/{id}` 가 읽을 때 계산한다 |
| 27 | §5.5 | 예산 결재의 반려가 무엇을 하는지 적지 않는다 | 런을 끊지 않고 "검수 요청을 올려라"는 지시로 잇는다 |
| 28 | §5.8 | S-5: 기존 런의 승인 대기 · 예산 초과도 결재로 | 작업 런이 아닌 런(대시보드 · CLI · 기존 캠페인 · 채팅 턴)은 `task_id` 없는 결재를 만든다 |
| 29 | §5.10 | S-14 "배치 필요로 알림", S-5 "코멘트와 수정 요청", N-3 "위험 명령 · 최종 검수는 알림에서 승인 불가" | §5.10 표의 4 · 5 · 9 다 — 배치 필요는 결재가 아니라 시스템 글과 푸시, 수정 요청은 `revise` · `rework` 결정 값과 `edits`, 알림에서 결정할 수 없는 것에 `structure_choice` · `question` 을 더했다. N-3 서버 쪽에서 `hire_recommendation` 도 알림에서 결정할 수 없다고 정했고 (§5.2), 알림 버튼은 앱이 종류로 추측하지 않고 서버가 푸시 `data.action` 으로 정하며 (§5.9), 답장은 새 경로 없이 기존 방 글 쓰기를 쓴다. 그 표의 나머지 항목은 위 23–28 과 같다 |

### 12.4 REST · WS · MCP (§6–§8)

| # | 태그 자리 | CAMPAIGN.md | 이 문서 |
|---|---|---|---|
| 30 | §6.2 | S-3: 기존 토큰 가드를 그대로 탄다 | 가드에 갈래가 하나 생긴다. 경로가 `/studio/` 로 시작하면 studio 인증 함수로 넘긴다 |
| 31 | §6.2 | S-3 · M-2 의 완료 기준은 401 | 기존 가드와 둘이 다르다 — 공용 토큰이 없는 서버에 원격으로 와도 403 이 아니라 401 이고, 루프백도 토큰을 요구한다(헤더가 없으면 401, 있으면 검사). `/studio` 가 아닌 기존 경로의 루프백 신뢰는 그대로다 (owner 확정, §12.6 의 5) |
| 32 | §6.2 | M-2: 기기별 토큰을 더한다 | 기기 토큰은 `/studio/*` 에서만 통한다. 앱이 읽는 기존 조회는 `/studio` 아래에 별칭을 둔다 |
| 33 | §6.4 | S-3 의 `/studio/*` 목록 | 목록에 없는 `POST /studio/guard/check` 를 더했다 |
| 34 | §6.4 | S-10: 물어보고 진행 목록은 "런을 멈추고 결재함으로 보낸다" | 작업 런이 아닌 턴에서는 결재를 만들지 않고 거부만 한다 (S-10 확정 — `guard.check`) |
| 35 | §6.6 | 직원이 파일을 붙이는 방법을 적지 않는다 | 도구 인자에 파일 경로를 넘기면 서버가 검사한 뒤 복사한다 |
| 36 | §8.4 | S-6: 정책 도구는 `policy_draft` 하나 | 제출 도구가 따로 없어 `submit` 인자로 받는다 |
| 48 | §6.4 | A-13: 앱 갱신은 '버전 + 파일'을 준다 (버전 이름 · 빌드 번호 · 크기 · sha256 · 올린 때 · 바뀐 점) | 읽는 곳과 고르는 법을 정했다. ① 폴더는 `Config.studio_app_dir` (기본 `~/.katae/app`, 저장소 밖) 이고 서버는 읽기만 한다. ② 버전 이름 · 빌드 번호 · 바뀐 점 한 줄은 폴더의 `latest.json` `{version_name, build_number, notes, apks: {abi: 파일명}}` 에서 읽는다 (owner 가 빌드 뒤 이 파일을 고친다). 올린 때는 고른 APK 의 파일 시각이다. ③ `--split-per-abi` 로 APK 가 여럿이면 `?abi` 로 `apks` 에서 고르고, 그 abi 가 없으면 `universal` 키, 그것도 없으면 `no_apk` 다. ④ 폴더 · `latest.json` · APK 가 없거나 깨진 것은 오류가 아니라 200 `{available: false, reason}` 이고, 내려받기만 404 다. ⑤ sha256 은 서버 메모리에 (파일 시각 · 크기)와 함께 두어 파일이 바뀔 때만 다시 계산한다 |
| 53 | §6.4 · §7.4 | T4: 채팅방 삭제 (CAMPAIGN 에 경로 이름이 없다) | 방 째 지우는 것은 단체방뿐이다 (`DELETE /studio/rooms/{id}`, `room.deleted`). 1:1 방은 글만 지우는 `POST /studio/rooms/{id}/clear` (`room.cleared`), 작업방 · 파트방 · 시스템 방은 `precondition_failed` 다. 도는 턴이 있으면 `failed` = `turn_running` · `turn_waiting` 으로 거절한다 |

### 12.5 직원 설정 · 권한 · 접점 (§9–§11)

| # | 태그 자리 | CAMPAIGN.md | 이 문서 |
|---|---|---|---|
| 37 | §9.2 | 전제 1 · S-7: MCP 목록은 하나이고 unreal 은 채팅에서 기본 끔 | 키를 둘로 나눴다 — `mcp`(작업 런)와 `chat_mcp`(채팅 · 읽기 턴, 기본 빈 목록) |
| 38 | §9.2 | S-8: 상한은 `model_cap` / `effort_cap`. 어디서 오는지 적지 않는다 | `employee.yaml` 에 `cap` 키를 더했다. 없으면 파트 기본값, 그다음 설정이다 |
| 39 | §11.2 | S-8: 덮어쓴 값의 상한은 `model_cap` / `effort_cap` | 엔진은 `stage_models` 로 고른 값에 상한을 걸지 않는다. studio 가 `create` 전에 본다 — 덮어쓴 칸이 넘으면 거부하고 `employee.yaml` 의 칸은 낮춘다 (S-8 확정) |
| 40 | §10.2 | §7 의 판정은 차단과 물어보기 둘 | 해석하지 못한 명령을 거부하는 `unparsed` 를 더했다 |
| 41 | §10.3 | §7 의 비밀: `.ssh` · 브라우저 프로필 · Claude 자격 증명 · `.katae/` · `relay.config.local.yaml` | 러너 임시 폴더의 `mcp.json`(다른 런의 행위자 토큰)을 더했다 |
| 42 | §10.3 | §7: 릴레이 서버 종료 | 서버 주소로 가는 셸의 요청을 모두 막는다. `/studio` 가 아닌 기존 경로는 루프백을 그대로 믿고, 공용 토큰을 얻은 셸은 `/studio/*` 에서 owner 로 통하기 때문이다 |
| 43 | §10.3 · §10.8 | §7: 서버 설정 · 가드 파일 수정 | `runs/studio/` 전체와 서버 루트의 `relay_agent/` 전체의 쓰기를 더했다. 서버 코드를 품은 프로젝트의 작업 런은 원본 작업으로 만들지 않는다 (§4.3 T3. 작업 런 쪽은 S-8 확정 — `pin_copy`. 훅의 쓰기 막기는 S-10 확정 — `guard.write_ctx`) |
| 44 | §10.4 | §7: 등록된 프로젝트 폴더 밖의 삭제 · 이동 | 이 런의 작업 폴더와 임시 폴더도 묻지 않는 루트로 본다 |
| 45 | §10.4 | §7: 1GB 넘는 다운로드 | 크기를 알 수 없으면 통과시킨다 |
| 46 | §11.1 | S-14: 기존 `campaign.py` 를 확장한다 | 기존 작업 상태 `skipped` 에 맞는 studio 상태가 없다 (S-14 확정 — 대응을 두지 않는다. studio 에는 건너뛰기가 없고, 그만둘 작업은 취소한다) |
| 47 | §9.4 | S-8: 고를 수 있는 값은 `GET /models` — 지금 쓸 수 있는 모델 전부 | 직원의 칸, 이번만 덮어쓴 칸, 릴레이 단계의 provider 는 Claude Code 만 받고 `/studio/models` 도 그 항목만 내준다. `auto` 의 후보에서도 다른 AI 의 단계를 가진 릴레이를 빼고, 실행 중의 대체(`alternates` · `fallback_chain`)도 Claude Code 로만 넘어간다. 다른 러너에는 가드 훅을 걸 수 없다 (S-10 확정 — `provider_unguarded` 와 `StageCall.claude_only`) |
| 49 | §4.8 · §5.9 | V-3: 서버 상시 가동 — 절전 방지, 재시작 후 직원 · 큐 · 결재 복구, 디스크 부족 경고 | 절전 방지는 작업 런의 슬롯과 채팅 · 읽기 턴이 도는 동안만 켜고(`studio/awake.py`) 전원 설정은 건드리지 않는다. 복구는 S-8 의 `work.recover` 가 이미 하는 것이라 새 코드 없이 시험으로 못박았다. 디스크 경고는 임계 `studio_disk_min_free_gb`(기본 5.0 GB)와 10분 주기를 이 문서가 정했고, 같은 부족을 한 번만 알리는 근거를 시스템 방의 글로 둔다 (`studio/disk.py`) |
| 50 | §4.6 · §5.9 · §6.4 | V-4: 사용량 안전망 — 한도 REST · 사용 창 알림 · 보류 옵션 | 한도는 새 설정 없이 기존 셋을 `PATCH /studio/machine/limits` 로 바꾸고 `relay.config.local.yaml` 에 적는다(재시작 뒤에도 남고 쓰기가 실패하면 바꾸지 않는다. 주석은 줄 교체가 되는 모양이면 보존한다). 알림 임계 `studio_usage_warn_at`(기본 0.90, 95% 전환보다 낮게)과 보류 `studio_usage_hold`(기본 꺼짐)는 이 문서가 정했다. 보류의 결재는 새 `kind` 없이 작업 없는 `budget`(`stop_reason = usage_hold`)이다 — 새 `kind` 는 DB 이전이 필요하다 (`studio/limits.py` · `studio/usage.py`) |
| 51 | §9.1 · §8.6 | (CAMPAIGN 에 없음) 모든 직원 턴에 하네스를 싣는다 | `studio/harness_root.py` 가 하네스 루트를 찾고 '하네스' 절을 만든다. 기존 `harness.locate` 와 다르다: `locate` 는 `bootstrap.bat` · `index_build.bat` 을 요구하고 `ValueError` 를 내며 프로젝트 생성 job 용이다. `resolve_root` 는 설정 → 정션의 두 단계 위 순으로 찾고 예외 없이 `Path` 나 None 을 돌려 프롬프트에 쓴다. 둘은 같은 설정 `studio_harness_root` 를 읽는다 |
| 52 | §10.1 · §11.2 | (CAMPAIGN 에 없음) 게임 프로젝트 런과 작업 런에 사용자 폴더의 하네스 스킬을 허용한다 | `projctx.game_root` 가 게임 프로젝트를 가린다(`*.uproject` · `ProjectSettings/ProjectVersion.txt`, 마커 없어도 됨). 인덱스 조회 명령은 하네스(정션 기준) SKILL.md 의 `python Tools/x.py` 형태에서 뽑고(`projctx.harness_commands`), `projctx.INDEX_COMMANDS`(`python Tools/gq.py` · `cindex.py` · `ue_q.py` · `ue_q`)는 대체 추정값이라 경로가 다르면 이 상수만 고치면 된다. `studio_harness_root` 설정은 이 경로에 쓰이지 않는다. 가드 판정 규칙은 그대로다 |

(T3, 태그 없음) 파트 지침(`studio/parts/*.md`)에 하네스 스킬 줄을 넣었다: 기획(문서)=`game-design-doc`, 프로그램=`game-onboard` · `game-architecture`(구현 전 구조 제시와 승인) · `game-testing`, 기획(세팅)=`game-design-doc` · `game-mcp`, 아트=`game-mcp`. 이미 채용된 직원의 `GUIDE.md` 는 고치지 않는다(새 직원이 파트 지침을 받을 때 실린다).

태그는 없지만 §10 이 CAMPAIGN.md §7 을 풀어 읽은 자리가 둘 있다. 차단 글머리 다섯 줄을 `rule_id` 일곱으로 나눴고(§10.3), `git clean -fdx` 를 `-f` 가 있는 `clean` 전부로 넓혔다(§10.4. S-10 확정 — `studio_guard.r_git_risky`. `-n` · `--dry-run` 이 함께 있으면 묻지 않는다).

### 12.6 owner 가 정한 것

본문의 "owner 확정 (§12.6 의 N)" 이 가리키는 16건이다. 1–12 는 본문이 잠정으로 두고 owner 에게 물었던 것이고, 13–16 은 owner 가 더한 것이다. 5 만 물을 때의 잠정과 다르게 정해졌다. 바꾸려면 이 문서를 먼저 고치고 owner 승인을 받는다 (§1.1 의 확정).

| # | 절 | 물은 것 | 정한 것 |
|---|---|---|---|
| 1 | §5.3 | H-6 의 정책 · 지침 수정안을 `hire` 결재의 `action` 으로 받아도 되는가 | 그렇게 받는다 |
| 2 | §5.5 | 재작업(T8) 뒤에도 승인된 위험 명령의 1회 허용이 남아도 되는가 | 남는다 |
| 3 | §5.5 | 예산 반려를 런 중단이 아니라 "검수 요청을 올려라"는 지시로 해도 되는가 | 지시로 한다 |
| 4 | §5.8 | 대시보드 · CLI · 기존 캠페인의 런도 결재함에 넣는가 | 넣는다 |
| 5 | §6.2 | `/studio/*` 가 헤더 없는 루프백 요청을 계속 받는가 | 받지 않는다. `/studio/*` 는 루프백 요청도 토큰을 요구하고 헤더가 없으면 401 이다. `/studio` 가 아닌 기존 경로의 루프백 신뢰는 그대로다. 직원이 스크립트로 서버를 불러 결재를 스스로 승인하는 길을 막는다 |
| 6 | §6.4 | 훅이 서버에 닿지 못할 때 명령을 거부하는 것이 맞는가 | 거부한다 |
| 7 | §7.2 | 테이블을 하나(`stream_events`) 더해도 되는가 | 더한다 |
| 8 | §10.2 | 해석하지 못한 명령을 거부해도 되는가 | 거부한다 |
| 9 | §10.4 | 임시 폴더를 삭제 · 이동을 묻지 않는 루트에 넣어도 되는가 | 넣는다 |
| 10 | §10.4 | 크기를 모르는 내려받기를 통과시켜도 되는가 | 통과시킨다 |
| 11 | §10.8 | `runs/studio/` 전체의 쓰기를 막아도 되는가 | 막는다 |
| 12 | §9.4 | 직원의 모델을 Claude Code 로만 한정해도 되는가 | 한정한다. 다른 AI 의 칸과 릴레이 단계는 받지 않는다 |
| 13 | §4 | 승인된 작업을 그만두는 종결 상태를 두는가 (owner 가 더했다) | 작업 상태 `cancelled` 를 둔다. owner 만 보내고, 런이 돌지 않는 `backlog` · `blocked` 에서만 간다 (T10 · T11). `working` 은 `stop` 을, `review` 는 되돌리기(T9)를 먼저 거친다. `blocked` 의 취소는 변경을 되돌릴 수 있을 때만 받는다 (§4.3 T11) |
| 14 | §3.6 | 직원 여럿이 한 방에서 대화하는 단체방을 두는가 (owner 가 더했다) | 방 종류 `group` 을 더한다 (마이그레이션 버전 3, 방 종류 5). owner 가 만들고 직원은 2–8명이다. owner 의 글은 멘션한 직원에게만, 없으면 방의 직원 전원에게 순서대로 턴을 준다. 직원 간 멘션의 왕복 한도는 작업방의 20 과 따로 6 이고, 닿으면 방이 멈춰 owner 가 이어 간다 (§4.6 · §6.4 · §6.6) |
| 15 | §3.4 · §5.5 | 아트 파트를 기본으로 두고, 인사가 새 파트를 함께 제안해 채용하는가 (owner 가 더했다) | 기본 파트 `art` 를 다섯 번째로 더한다 (마이그레이션 버전 4. 직원 없는 선택 파트). 채용 초안이 `part_*` 로 새 파트를 함께 제안하면 승인 한 번에 파트 · 파트방 · 직원이 생기고 반려하면 아무것도 생기지 않는다 |
| 16 | §5.1–§5.3 | 인사가 채용 권장을 올리는가 (owner 가 더했다) | 결재 종류 `hire_recommendation` 을 더한다 (결재 10종, 마이그레이션 버전 5). 계기는 서버가 규칙으로 찾고 판단은 인사의 1:1 방 채팅 턴(판단 턴, §8.4)이 한다. 결정은 `interview` · `skip` 이고 알림에서는 결정하지 않는다. 도구가 12개에서 14개가 된다 (`staff_request` · `hire_recommend`, §8.4) |

---

## 13. 작업별 인용 절 색인

CAMPAIGN.md 의 서버 쪽 작업 31개(S-1–S-14, M-1–M-8, H-1–H-6, N-1–N-3)가 인용할 절이다. "주로 볼 절"은 그 작업이 구현하는 계약이고 "함께 볼 절"은 그 계약이 기대는 이름과 접점이다. 인용할 때는 소절까지 적는다 (예: §4.3 T6, §10.3 의 5). 앱(A) · 검증(V) 작업은 §6 · §7 을 인용한다.

### 13.1 S — 서버

| 작업 | 주로 볼 절 | 함께 볼 절 |
|---|---|---|
| S-1 저장소와 스키마 | §3 (3.1–3.16), §2.2 | §1.5 (작업 상태 8개 — `cancelled` 까지 §3.10 의 CHECK 에 든다), §2.1, §7.2 (`stream_events`, 버전 2), §11.1 |
| S-2 작업 · 캠페인 상태기계 | §4.1–4.5 (상태 8개 · 64칸. 작업 취소 T10 · T11 → `cancelled`), §4.9, §4.10 | §1.3 · §1.5, §3.10–3.12, §5.1 (T11 이 닫는 결재), §7.5, §12.6 의 13 |
| S-3 REST API | §6.1–6.4 (§6.2 의 루프백 토큰, §6.4 의 작업 `cancel`), §6.6 | §4.3 T10 · T11, §4.5 (오류 코드), §11.3 (`guard` 의 갈래 자리), §12.6 의 5 · 13 |
| S-4 실시간 스트림 | §7 (7.1–7.6) | §3.16, §6.2 (토큰 — 루프백도 요구한다), §6.4 (`/studio/runs/{run_id}/live`), §11.3 |
| S-5 결재함 | §5 (5.1–5.10), §3.13 | §4.3 (T1 · T2 · T4 · T5 · T11), §4.7, §6.4 (결재 경로), §11.3 |
| S-6 studio MCP 서버 | §8 (8.1–8.5) | §5.7, §6.6 (직원의 첨부), §11.2 (`READ_ONLY_MCP`), §11.3, §11.4 |
| S-7 직원 실행기 — 채팅 | §9.1–9.4, §9.6 | §2.2, §3.7, §3.15 (시드), §4.9, §6.6, §7.4 (`run.live`), §8.2, §9.5 (채팅 · 읽기 턴, 프로젝트 맥락이 없는 런의 MCP), §11.1, §11.2 (MCP 한정 인자 · 작업 폴더) |
| S-8 작업 실행 · 큐 · 폴더 락 | §4.3 T3, §4.6–4.8, §9.5 | §6.4 (`start` · `stop` · `/studio/models`), §8.3 (`end_turn` 뒤 `RelayEngine.cancel` 로 끊는 대기 시간 — `studio/cutoff.py`), §10.8 의 8 · 9, §11.2 (공개 라우팅 메서드 · `copy` 고정 인자) |
| S-9 직원 간 소통 | §4.6 (왕복 한도), §8.4 (`room_post`), §8.5 | §3.7, §4.5, §5.3 (`exchange_limit`), §5.7, §6.6, §11.2 (`interject`) |
| S-10 권한 프로파일과 치명 가드 | §10 (10.1–10.8), §6.4 (가드) | §2.1 (`studio/guard.py` · `studio_guard.py`), §2.2 (감사 로그의 자리), §4.3 T3 (서버 코드를 품은 프로젝트), §4.5, §5.3 · §5.5 · §5.6 (`risky_command`), §8.2, §8.3 (가드의 T4 뒤처리), §9.5 의 5 (가), §11.4 (연결 지점 6) |
| S-11 하네스 준수 게이트 | §4.3 T6, §5.4 (`checklist` · `harness_checks`), §10.7 '런별 모음' (`studio/runlog.py`), §11.2 (`_emit_activity`) | §5.3 (`structure_choice`), §8.4 (`task_request_review` · `ask_owner`) |
| S-12 검수 패키지 · 재작업 · 후속 작업 | §4.3 T6–T9 · T11 (런 정리 · 되돌리기), §5.4, §5.5 (`final_review`) | §4.7, §5.6, §6.4 (결재 경로 · 런 별칭), §8.4 (`followup_propose`), §11.2 |
| S-13 진척 집계와 일일 보고 | §6.4 (`progress` · `/studio/stats` · `/studio/growth` 와 경험치 · 레벨 · 업적 규칙표), §5.9 (일일 보고) | §3.9, §8.5 (`system` 방) |
| S-14 캠페인 자동 진행과 진행 방식 | §4.10, §4.6 | §3.9, §3.11, §4.3 T3, §5.9 (배치 필요 · 선행 반려 · 선행 거절), §6.4 (캠페인 경로), §7.4 (`campaign.updated`), §7.6, §11.1, §11.2 |

### 13.2 M · H · N

| 작업 | 주로 볼 절 | 함께 볼 절 |
|---|---|---|
| M-1 컴퓨터 진단 API | §6.4 (`GET /studio/machine`) | §6.1, §11.3 (`GET /providers`) |
| M-2 기기 페어링 | §6.5, §6.2, §3.14 | §3.16 (페어링 코드), §6.4 (기기 경로), §7.1 |
| M-3 UE 프로젝트 생성기 | §6.4 (`POST /studio/projects/create`, 생성 작업) | §3.3, §7.4 (`job.step`) |
| M-4 플러그인 설치기 | §6.4 (`POST /studio/projects/create` 의 `plugins[]`) | §3.3, §7.4 (`job.step` 의 `plugins`) |
| M-5 하네스 · 인덱스 · 릴레이 등록 · git | §6.4 (`POST /studio/projects/create` 의 `harness`) | §3.3, §7.4 (`job.step` 의 `harness` · `register`), §11.1 (`RepoRegistry`) |
| M-6 기존 프로젝트 가져오기 | §6.4 (`POST /studio/projects`) | §3.3, §11.1 (`RepoRegistry.get` · `match`) |
| M-7 에디터 원격 제어 | §6.4 (에디터 경로), §4.3 T3 (에디터 검사) | §4.5, §4.7, §5.3 (`editor_unresponsive`), §6.6 |
| M-8 진행 표시와 실패 정리 | §7.4 (`job.step` · `job.log` · `job.finished`), §6.4 (`/studio/jobs/{id}`) | §2.2, §5.9 (푸시) |
| H-1 파트 템플릿 | §3.4, §6.4 (파트 경로) | §3.15 (시드), §9.2 (`default_models_json` 의 모양) |
| H-2 인사 직원 | §5.3 · §5.5 (`hire`), §8.4 (인사 전용 도구) | §3.5, §8.5, §9.2–9.4 |
| H-3 직원 수명주기와 지침 이력 | §6.4 (직원 경로), §9.6 | §2.2 · §2.3, §3.5, §4.9, §5.5 (`guide_amend`), §7.4 (`employee.updated`) |
| H-4 직원 메모리 | §9.1 (`MEMORY.md` 의 상한) | §2.2, §5.5 (`rework`), §6.4 (`PUT …/files/{name}`) |
| H-5 파트 간 인계 형식 | §8.4 (`room_post` 의 `handoff`) | §7.6, §8.3 |
| H-6 결재 기준 | §5.3 (`pre_approval` · `hire` 의 `action`), §5.5 (`judgment_marks`) | §2.2 (`POLICY.md`), §5.2, §6.4 (정책 경로), §8.4 (`ask_owner` · `policy_draft`), §9.1 |
| N-1 FCM 발송기 | §5.9 (푸시) | §3.14, §5.1, §11.3 (`Notifier`) |
| N-2 알림 정책 | §5.9 (등급 · 재알림) | §3.6, §3.13 (`reminded_at`), §3.14, §6.4 (`PATCH /studio/rooms/{id}` · `PATCH /studio/devices/{id}` 의 `settings`) |
| N-3 알림 액션 | §5.2 (알림에서 결정), §5.9 (딥링크 · 알림 액션) | §5.1 (`via`), §6.4 (`decide`) |

**owner 요청 (2026-10-12) T1–T5** — 31개 작업(CAMPAIGN.md)이 아니라 owner 가 더한 요청이다 (§12.6 의 14–16).

| 작업 | 주로 볼 절 | 함께 볼 절 |
|---|---|---|
| T1 단체방의 저장과 REST | §3.2 (버전 3), §3.6, §3.7, §6.4 | §1.5, §7.6 |
| T2 단체방의 턴 규칙 | §4.6, §6.6 | §7.4, §7.6, §8.4, §9.5 |
| T3 아트 파트와 새 파트를 함께 제안하는 채용 | §3.4 (버전 4), §3.15, §5.5 | §5.3, §8.4 |
| T4 채용 권장의 계기 · 결재 종류 · 되풀이 막기 | §3.2 (버전 5), §3.13, §5.1–§5.3 | §3.16, §5.5, §5.9 |
| T5 인사의 판단 턴과 두 도구, 면담으로 잇기 | §5.3, §5.5, §8.4 | §8.5, §9.5 |

- M-3 · M-4 · M-5 가 이 문서에서 인용할 것은 REST 경로와 생성 작업의 이벤트뿐이다. 템플릿, 플러그인 복사, 포트 배정, 하네스 실행의 내부는 CAMPAIGN.md 의 작업 명세가 기준이다.
- 작업이 절을 구현하다 계약을 바꿔야 하면 이 문서를 먼저 고친다 (§1.1 의 "확정"). "잠정"과 "확인 필요"는 괄호 안의 작업이 구현하면서 확정하고 이 문서에 되적는다. 작업이 끝난 뒤에도 실제 Claude Code 나 실기기로만 볼 수 있어 남은 "확인 필요"는 괄호가 `VERIFICATION.md` 의 줄을 가리킨다 (§1.1).

### 13.3 P-5 산출물 8항목

CAMPAIGN.md 의 P-5 명세가 적은 여덟 가지가 들어 있는 절이다.

| # | P-5 의 항목 | 절 |
|---|---|---|
| 1 | 테이블 정의 | §3 |
| 2 | 상태 전이 표 (누가 · 언제 · 부수 효과) | §4 |
| 3 | 결재 종류별 payload | §5.3 · §5.4 |
| 4 | REST · WS 계약 | §6 · §7 |
| 5 | studio MCP 도구 시그니처 | §8.4 |
| 6 | `employee.yaml` 형식 (모델 · 노력치 포함) | §9 |
| 7 | 권한 프로파일 | §10 |
| 8 | 디렉터리 배치 | §2 |
