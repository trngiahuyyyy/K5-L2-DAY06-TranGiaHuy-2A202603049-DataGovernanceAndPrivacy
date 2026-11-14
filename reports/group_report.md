# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Điền đầy đủ, cụ thể, không viết chung chung. Mọi con số phải lấy từ file do script sinh ra
> (`reports/metrics_*.json`, `reports/table1_baseline_vs_poc.md`) — không gõ tay, không làm tròn mất đơn vị.
> 3–5 trang (hoặc README + report.md tương đương).

## 00 · Header

| Mục | Giá trị |
|---|---|
| Group ID | G08 |
| Challenge | T08 · Retention & Delete Request Simulator |
| Thành viên (5–6) + vai trò | 1. Trần Gia Huy (Lead/Integrator) · 2. Trần Gia Huy (Data & fixtures) · 3. Trần Gia Huy (Core algorithm) · 4. Trần Gia Huy (QA/metric) · 5. Trần Gia Huy (Review + report) · 6. — (nhóm 1 người, kiêm nhiệm mọi vai) |
| Git URL | https://github.com/trngiahuyyyy/K5-L2-DAY06-TranGiaHuy-2A202603049-DataGovernanceAndPrivacy |
| Tag `demo-180m` (commit) | 80eb555 |
| Tag `final-24h` (commit) | 55ee951 |
| Deadline (giờ VN) | 12:00 AM 15/11/2026 |

## 01 · Problem statement

- Ai bị ảnh hưởng / ai chịu hậu quả khi hệ thống sai? Chủ thể dữ liệu (người xuất hiện trong video lái xe) đã gửi yêu cầu xóa: nếu bỏ sót thì khuôn mặt, embedding và bản sao lưu vẫn còn; nếu xóa nhầm thì mất dữ liệu đang bị legal hold hoặc dữ liệu của người khác. Đội vận hành dữ liệu và DPO chịu hậu quả pháp lý.
- Điểm nghẽn cụ thể: dữ liệu của một người nằm ở nhiều tầng (raw video → frame → face crop → embedding → annotation, cộng backup và training export), nên xóa "một file raw theo tên" bỏ sót phần lớn bản sao nhận diện được và có thể xóa luôn bản đang bị giữ theo nghĩa vụ pháp lý.
- Đầu vào → đầu ra: `data_graph.json` (212 artefact mock) + `requests.json` (10 yêu cầu xóa) → `out/actions.csv` (`request_id, artefact_id, action ∈ {delete, retain_legal_hold, escalate}`), `audit_log.jsonl`, `review_queue.csv`, `decision.json`.
- Tiêu chí GO / HOLD / REWORK (chốt trước khi chạy eval): REWORK nếu đồ thị vi phạm toàn vẹn (id trùng, parent không tồn tại); HOLD nếu còn bất kỳ hàng `escalate` chưa được người duyệt xử lý; GO khi không còn hàng `escalate` (hàng `retain_legal_hold` được ghi nhận, không chặn).
- Giả định của nhóm (cần DPO/legal/domain owner xác nhận): (1) backup của artefact đang bị hold cũng không được xóa tự động; (2) artefact nhiều chủ thể phải qua người duyệt, kể cả khi mọi chủ thể đều đã yêu cầu xóa; (3) artefact dẫn xuất thuộc chủ thể khác không bị xóa; (4) `acl` không chứa nhóm `data-platform` nghĩa là chưa có quyền xóa.

## 02 · Data & provenance

| Split | N | Nguồn / link | License | Synthetic? |
|---|---|---|---|---|
| dev | 212 artefact, 10 yêu cầu (fixture không chia split công khai) | `fixtures/t08_deletion/` (`mock-graph-1`) | theo cột `license` trong `fixtures/t08_deletion/manifest.csv` | Có |
| eval / heldout | không có ở máy học viên (`gt/gt_actions.csv` do giảng viên giữ) | repo giảng viên (private) | — | Có |

- Schema & class mapping: `artefact_id, type, subject_ids, parent_ids, uri, owner, acl, legal_hold, origin_known, created_at`; type ∈ {raw_video, backup, frame, face_crop, embedding, annotation, training_export}; action ∈ {delete, retain_legal_hold, escalate}.
- Seed: 42 · Cách chia split (theo scene/group/segment?): không áp dụng; thuật toán xác định, không huấn luyện và không chỉnh tham số theo GT.
- `manifest.csv` verified (sha256): có — lệnh: `python -m tools.verify_manifests` (đã kiểm bằng `python -m tools.preflight`: "hashes match" cho 14 fixture, sau khi đặt `core.autocrlf=false` để sửa lỗi CRLF khi clone trên Windows)
- Dữ liệu thật dùng (nếu có) và cách xây GT cho nó: không dùng (đề cấm xóa dữ liệu thật).

## 03 · Research & design

- 2–3 references (URL + ngày xem) và **vì sao** chọn kỹ thuật: (1) đề `docs/tracks/T08.md` (repo lab, 14/11/2026): gợi ý BFS lineage và thứ tự ưu tiên legal hold > permission > multi-subject > delete, nên chọn luật xác định thay vì học máy vì xóa nhầm không bù được; (2) GDPR Điều 17(3) https://gdpr-info.eu/art-17-gdpr/ và Nghị định 356/2025/NĐ-CP (đề dẫn): quyền xóa có ngoại lệ khi còn nghĩa vụ pháp lý lưu giữ, nên phải ghi `reason_code` thay vì xóa mù quáng; (3) California DROP (đề dẫn): một yêu cầu xóa đi qua nhiều bên giữ dữ liệu, tương tự một yêu cầu của ta đi qua nhiều tầng artefact.
- Sơ đồ data flow (input → process → rank/QC → review → decision → report): `data_graph.json + requests.json` → load (kiểm sha256 + toàn vẹn đồ thị) → process (artefact gắn subject + BFS xuống con cháu, áp luật ưu tiên cố định, 1 hàng/(request, artefact)) → rank (mọi hàng `escalate` vào `review_queue.csv`, xếp theo rủi ro) → review (`review_queue_reviewed.csv`, override có chốt chặn) → decide (GO/HOLD/REWORK + `reason_code` + owner) → `actions.csv` + `audit_log.jsonl` + `decision.json`.
- Thành phần offline (model/weights + checksum + license nếu có): không có model hay trọng số; chỉ Python chuẩn + pandas, không truy cập mạng.
- Trade-offs đã cân nhắc: an toàn hơn đầy đủ (mọi ca không chắc chắn đều `escalate`, đánh đổi Completeness thấp hơn và tải duyệt cao hơn, 36/164 hàng ≈ 22%); backup của artefact bị hold chọn `escalate` (không `delete`, không `retain_legal_hold`); con cháu của chủ thể khác xuất `escalate` để người duyệt thấy lineage; chưa tự động gộp nhiều yêu cầu để xóa artefact chung vì rủi ro cao.

## 04 · Baseline vs PoC

- Baseline (mô tả, version/config, commit): `src/t08_baseline.py`, commit 80eb555; với mỗi yêu cầu, xóa raw video (`.mp4`) có tên file chứa `subject_id`; không truy lineage, không kiểm legal hold, không duyệt.
- PoC (mô tả, version/config, commit): `src/pipeline.py` + `src/t08_policy.py`, commit 404c5f0; BFS lineage, luật ưu tiên cố định, hàng đợi duyệt, audit log, chốt chặn legal hold; `configs/lab.yaml` (track t08, seed 42, `dry_run: true`).
- Cùng budget / cùng heldout / cùng seed: xác nhận có (cùng `fixtures/t08_deletion`, cùng 10 yêu cầu, seed 42).

**Bảng 1 · Baseline vs PoC & hard slices** — dán output của
`python -m src.compare reports/metrics_baseline.json reports/metrics_poc.json`:

| Metric (unit) | Baseline | PoC | Δ & chú thích |
|---|---|---|---|
| Primary: Deletion Completeness (tỉ lệ) | chưa tính được | chưa tính được | không có `gt/` ở máy học viên nên không chạy được `src.evaluate`; giảng viên chấm; higher = tốt |
| Hard-slice: dữ liệu nhận diện bị bỏ sót (frame, face_crop, embedding, backup; số cặp) | 103 | 0 | −103; N=103; số từ `tools/t08_selfcheck.py` (không cần nhãn), lower = tốt |
| Review@B / miss / FP | B=0; miss 148/158; xóa nhầm 2 | B=36; miss 0/158; xóa nhầm 0 | baseline xóa cả 2 raw đang bị legal hold (`a0043`, `a0099`); B=36 là toàn bộ `escalate` |
| Runtime / 100 items | 0,029 s/lần chạy (10 hàng) | 0,10–0,31 s/lần chạy (164 hàng) | ≈ 0,06–0,19 s/100 hàng (PoC); CPU=Intel Core, Windows 10, Python 3.10.11 |

## 05 · Evaluation protocol

- Công thức primary metric, đơn vị đánh giá, TP/FP/FN, denominator: Deletion Completeness = cặp (request, artefact) GT `expected_action == delete` mà ta xuất `delete` / số cặp GT `expected_action == delete`; False-delete = cặp GT ngoại lệ ta xóa + cặp ta xóa nằm ngoài GT (xem `reports/metric_contract.md`)
- Threshold / budget B / K và lúc chốt: không có ngưỡng số; thứ tự luật cố định ở commit 80eb555 (14/11/2026 15:56), trước khi có bất kỳ kết quả chấm nào; B = toàn bộ hàng `escalate` (36)
- Độ bất định (CI/bootstrap/variance) khi N nhỏ: N = 10 yêu cầu, thuật toán xác định nên không có phương sai theo seed; báo số đếm tuyệt đối, không suy luận thống kê từ 10 yêu cầu; chạy 2 lần cho `actions.csv` giống từng byte (test `test_real_fixture_is_idempotent`)
- Lệnh evaluate chính xác: `python -m src.evaluate --track t08 --pred out/actions.csv --out reports/metrics_poc.json`

## 06 · Result slices

| Slice | N | Baseline | PoC | Ghi chú |
|---|---|---|---|---|
| LEGAL_HOLD | 2 | xóa cả 2 (vi phạm) | giữ cả 2 (`retain_legal_hold`) | `a0043` (DR-002, LH-2026-03), `a0099` (DR-005, LH-2026-07) |
| Ngoại lệ chuyển người duyệt (`escalate`) | 36 | không có hàng nào | 36 hàng `escalate` | gồm SHARED_EXPORT 12, MULTI_SUBJECT 12, DERIVED_OTHER_SUBJECT 6, ORIGIN_UNKNOWN 3, COPY_OF_HELD_ARTEFACT 2, NO_PERMISSION 1; 2 hàng MULTI_SUBJECT được người duyệt chuyển thành `delete` |

- Failure case 1 (ảnh/metadata + root cause): baseline xóa nhầm raw đang legal hold: `DR-002 / a0043` (`drive_subj_03_003.mp4`, `legal_hold = LH-2026-03`) và `DR-005 / a0099`; root cause: xóa theo tên file, không kiểm trường `legal_hold`.
- Failure case 2: baseline để sót 103 cặp frame/face_crop/embedding/backup của chủ thể (ví dụ chuỗi raw → frame → face_crop → embedding của subj_05); root cause: không truy ngược lineage nên chỉ xóa raw nhưng giữ bản sao và embedding định danh. Giới hạn của PoC: `a0211` là export 10 chủ thể, 8 người đã yêu cầu xóa nhưng subj_06 và subj_10 chưa, nên PoC `escalate` thay vì xóa; nếu GT coi các ca như vậy là `delete` thì Completeness thấp hơn nhưng False-delete không tăng.
- Latency / compute (máy, CPU, ms/item): Intel Core, Windows 10, Python 3.10.11; 0,10–0,31 s cho 164 hàng (≈ 0,6–1,9 ms/hàng), theo `run_info.json`.

## 07 · Human & decision

- Review queue (file, B, cách ưu tiên): `out/poc/review_queue.csv`, B = 36 (toàn bộ `escalate`), xếp theo rủi ro: ORIGIN_UNKNOWN/NO_PERMISSION 0,90 > SHARED_EXPORT 0,80 > MULTI_SUBJECT 0,70 > COPY_OF_HELD 0,60 > DERIVED_OTHER 0,50.
- Cách override + audit log (`out/audit_log.jsonl`): người duyệt điền `reviewer, reviewer_decision, override_value, review_note` vào `review_queue_reviewed.csv`; không thể ghi đè `retain_legal_hold`, override sang `delete` bắt buộc có `review_note`; mọi quyết định ghi vào `audit_log.jsonl` với `item_id = artefact_id`. Bằng chứng một vòng duyệt (7 quyết định + 1 ca thử ghi đè legal hold bị `review_edit_refused`): `demo/T08_review_queue_reviewed.csv`, `demo/T08_review_audit_excerpt.jsonl`.
- Decision policy + `reason_code` + owner: `GO / HOLD / REWORK` với reason_code ∈ {DELETION_PLAN_CLEAR, PENDING_EXCEPTION_REVIEW, GRAPH_INTEGRITY}; owner = "Tran Gia Huy - reviewer"; lần chạy hiện tại ra `HOLD / PENDING_EXCEPTION_REVIEW`.
- Khi nào HOLD/REWORK thay vì tự động release: HOLD khi còn ngoại lệ chưa duyệt; REWORK khi đồ thị vi phạm toàn vẹn; hệ thống không bao giờ tự xóa một ngoại lệ.

## 08 · Reproduce & tests

```bash
make setup          # hoặc: python -m venv .venv && pip install -r requirements.txt
python -m src.main --input fixtures/t08_deletion --config configs/lab.yaml --output out/ --offline --seed 42
python -m src.evaluate --track t08 --pred out/actions.csv --out reports/metrics_poc.json
pytest -q
```

- Output của `pytest -q`: 0 failed; các test cần `gt/` của giảng viên được skip; 11 test T08 trong `tests/test_t08.py` đều pass (8 test logic trên đồ thị tổng hợp + 3 test end-to-end trên fixture thật).
- Máy / CPU / OS / Python: Intel Core / Windows 10 (build 26200) / Python 3.10.11 · git sha: 404c5f0

## 09 · Limitations & ethics

- Rủi ro pháp lý / PII / license: dữ liệu hoàn toàn mô phỏng, không xóa dữ liệu thật; không có PII thật; license theo `manifest.csv` của fixture.
- Điều chưa biết; false alarms; nếu scale 10× thì sao: GT của giảng viên có thể coi một số ca `escalate` của ta là `delete` (hoặc ngược lại); nhánh "danh tính chưa xác minh" chưa được fixture kích hoạt (cả 10 yêu cầu đều đã xác minh), chỉ kiểm bằng unit test. Scale 10×: duyệt đồ thị tuyến tính nên không nghẽn; điểm nghẽn là tải duyệt thủ công (≈ 22% hàng là `escalate`) và `audit_log.jsonl` ghi nối tiếp qua các lần chạy.
- Không tuyên bố "đạt GDPR / luật Việt Nam" từ checklist tự động; giả định cần DPO/legal xác nhận: các quy tắc legal hold, bản sao của artefact bị hold, đa chủ thể và quyền xóa theo Luật 91/2025, Nghị định 356/2025 và GDPR Điều 17(3) chỉ là giả định kỹ thuật, chưa được đối chiếu số điều với văn bản gốc.
- Kế hoạch cải tiến: gộp yêu cầu để cho phép xóa artefact chung khi mọi chủ thể đều đã yêu cầu; hẹn xóa tự động khi hết legal hold; bảng điều khiển SLA theo `deadline` và thông báo; chạy tiếp được sau khi job bị ngắt. Công cụ hỗ trợ: Claude (Anthropic) hỗ trợ thiết kế, viết mã và soạn báo cáo; tác giả tự chạy, kiểm tra và chịu trách nhiệm.

## 10 · Team contribution

**Bảng 2 · Individual contribution evidence** (đối chiếu với `git shortlog -sne --all` và `reports/team_contributions.csv`)

| Học viên | Git author/email | Module / vai trò | Commit SHA / diff chứng minh | Trình bày? |
|---|---|---|---|---|
| Member 1 | Tran Gia Huy / 26ai.huytg2@vinuni.edu.vn | Integration / CLI | af1a23a (`configs/lab.yaml`) | Có |
| Member 2 | Tran Gia Huy (Member 1 kiêm nhiệm) | Data + GT | 80eb555 (`tools/t08_selfcheck.py`) | Không |
| Member 3 | Tran Gia Huy (Member 1 kiêm nhiệm) | Core algorithm | 80eb555, 404c5f0 (`src/pipeline.py`, `src/t08_policy.py`, `src/t08_baseline.py`) | Không |
| Member 4 | Tran Gia Huy (Member 1 kiêm nhiệm) | Metric + tests | 80eb555, 404c5f0 (`tests/test_t08.py`) | Không |
| Member 5 | Tran Gia Huy (Member 1 kiêm nhiệm) | Review queue + report | 404c5f0 (`demo/`); commit báo cáo: ____ | Không |
| Member 6 (nếu có) | — (không có) | Research / ablation / debate | — | — |

## 11 · References

| URL | Ngày xem | 1–2 dòng ý nghĩa (không chép nguyên văn) |
|---|---|---|
| https://github.com/VinUni-AI20k/K5-Day6-L2-DataGovernanceAndPrivacy | 14/11/2026 | Đề bài T08: hợp đồng đầu ra, metric và thứ tự ưu tiên luật. |
| https://gdpr-info.eu/art-17-gdpr/ | 14/11/2026 | Quyền xóa và ngoại lệ ở khoản 3 khi còn nghĩa vụ pháp lý lưu giữ. |
| https://thuvienphapluat.vn/van-ban/Quyen-dan-su/Nghi-dinh-356-2025-ND-CP-huong-dan-Luat-Bao-ve-du-lieu-ca-nhan-687428.aspx | 14/11/2026 | Nghị định 356/2025/NĐ-CP (31/12/2025) hướng dẫn Luật Bảo vệ dữ liệu cá nhân 91/2025/QH15, hiệu lực 01/01/2026. |

---

**Bảng 3 · Checklist chốt khi nộp**

- [ ] Link repo và tag cuối cùng trong README; input mẫu không chứa PII thật.
- [ ] Đúng 1 lệnh run, 1 lệnh evaluate, 1 lệnh tests; không cần download khi chạy.
- [ ] Có baseline/PoC metrics, ≥1 failure case, reviewer evidence, decision log.
- [ ] Có license/source provenance, data splits, seed, offline environment, GT/eval N.
- [ ] Mỗi thành viên có ≥1 commit thực chất và được nêu minh bạch trong bảng trên.
