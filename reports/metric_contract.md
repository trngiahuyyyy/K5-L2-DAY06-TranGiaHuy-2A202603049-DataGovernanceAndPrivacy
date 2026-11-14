# Metric contract (khai báo bắt buộc — chốt ở phút 30, trước khi chạy eval)

> Các metric chuẩn đã được cài trong `src/metrics/tracks.py`. File này là nơi nhóm **cam kết** cách dùng chúng.
> Không đổi testset, threshold hay trọng số sau khi đã thấy kết quả eval.

| Mục | Khai báo của nhóm |
|---|---|
| Track | T08 |
| Primary metric (tên đúng như evaluator in ra) | Deletion Completeness |
| Tăng tốt hay giảm tốt? | higher (guardrail đi kèm: False-delete count, lower, mục tiêu 0) |
| Đơn vị đánh giá (image / region / pair / release / batch / case …) | cặp (request_id, artefact_id) |
| Công thức + numerator / denominator | numerator = số cặp GT có `expected_action == delete` mà ta xuất `delete`; denominator = số cặp GT có `expected_action == delete`. False-delete = cặp GT ngoại lệ ta xóa + cặp ta xóa nằm ngoài GT |
| Cách ghép prediction ↔ GT (IoU? id? tolerance?) | theo id: khóa (request_id, artefact_id), left-join từ GT; hàng GT mà ta không xuất tính là chưa xóa; không có tolerance |
| Candidate pool cố định | `fixtures/t08_deletion/` (212 artefact, 10 yêu cầu xóa) |
| Review budget B / K | B = toàn bộ hàng `escalate` (36 ở lần chạy hiện tại), xếp theo mức rủi ro |
| Seed | 42 |
| Threshold policy (chọn trên dev như thế nào) | không có ngưỡng số; thứ tự luật cố định: legal hold > bản sao của artefact bị hold > thiếu quyền (acl) > nguồn gốc không rõ > nhiều chủ thể > danh tính chưa xác minh > delete; không chỉnh theo GT |
| Eval split (không dùng để tune) | không có split công khai; GT (`gt/gt_actions.csv`) chỉ giảng viên giữ, học viên không có nên không dùng để chỉnh luật |
| Chỉ số lỗi xấu (miss / false alarm / false reject) | False-delete (xóa nhầm ngoại lệ hoặc ngoài phạm vi); miss = artefact của chủ thể không có hàng trong `actions.csv`; false escalate = ca lẽ ra xóa được bị đẩy sang người duyệt |
| Slices sẽ báo cáo (≥2) | theo `reason_code` (LEGAL_HOLD, COPY_OF_HELD_ARTEFACT, NO_PERMISSION, ORIGIN_UNKNOWN, MULTI_SUBJECT, SHARED_EXPORT, DERIVED_OTHER_SUBJECT) và theo loại artefact |
| Cách báo độ bất định khi N nhỏ (Wilson CI / bootstrap) | N = 10 yêu cầu, thuật toán xác định nên không có phương sai theo seed; báo số đếm tuyệt đối, nếu báo tỉ lệ thì kèm Wilson CI 95% |
| Ngày giờ chốt + người chốt | 14/11/2026 15:56 (commit 80eb555) · Trần Gia Huy |

Nếu baseline = 0 thì ghi rõ "baseline = 0" thay vì phần trăm cải thiện.
