from flask import jsonify

LABELS = {
    'FILE_TOO_LARGE':'文件超过 256 KiB', 'FILE_EMPTY':'文件没有内容', 'FILE_BLANK':'文件只有空白',
    'HEADER_ONLY':'文件只有标题行', 'HEADER_MISMATCH':'列名与字段字典不一致', 'ENCODING_INVALID':'请使用 UTF-8 CSV',
    'CSV_MALFORMED':'CSV 引号或行格式异常', 'ROW_LIMIT':'文件超过 2000 行', 'FILE_REQUIRED':'请选择 CSV 文件',
    'RUN_NOT_FOUND':'批次不存在', 'REVIEW_FIELDS':'请填写 3 至 200 字复核意见', 'STALE_REVIEW':'批次已更新，请刷新',
    'RUN_FINALIZED':'批次已签发', 'UNRESOLVED_ISSUES':'存在待处理差异', 'SAMPLE_UNKNOWN':'样例不存在', 'STAGE_UNKNOWN':'处理阶段不存在',
    'SLOT_TAKEN':'该执行槽已被其他批次领取', 'TASK_NOT_ISSUED':'任务尚未签发，不能领取',
    'UPSTREAM_FAILED':'上游分区回执失败，依赖分区不能领取', 'RECEIPT_REQUIRED':'超时且未收到完工回执，执行槽不能释放',
    'CLAIM_NOT_FOUND':'领槽记录不存在', 'CLAIM_FIELDS':'领槽或回执字段不完整',
    'IDEMPOTENCY_CONFLICT':'相同请求编号对应不同内容',
}

NOT_FOUND = {'RUN_NOT_FOUND', 'SAMPLE_UNKNOWN', 'STAGE_UNKNOWN', 'CLAIM_NOT_FOUND'}
CONFLICT = {'STALE_REVIEW', 'RUN_FINALIZED', 'UNRESOLVED_ISSUES', 'SLOT_TAKEN',
            'TASK_NOT_ISSUED', 'UPSTREAM_FAILED', 'RECEIPT_REQUIRED', 'IDEMPOTENCY_CONFLICT'}
TOO_LARGE = {'FILE_TOO_LARGE'}


def response(error):
    code = str(error.code)  # 取规范码，避免 FlexStr 别名影响字典/状态判断
    if code in NOT_FOUND:
        status = 404
    elif code in CONFLICT:
        status = 409
    elif code in TOO_LARGE:
        status = 413
    else:
        status = 422
    body = {'code': code, 'error': code, 'error_code': code, 'reason': code,
            'detail': LABELS.get(code, LABELS.get(error.code, code)),
            'message': LABELS.get(code, LABELS.get(error.code, code))}
    holder = getattr(error, 'holder', None)
    if holder:
        body['holder'] = holder
    return jsonify(body), status
