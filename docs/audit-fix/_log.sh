#!/usr/bin/env bash
# 追加一条极简日志：log_line "问题" "解决方案" "修复结果"
# 注意：路径写死为项目内 docs/audit-fix，避免 source 时 $0 指向调用方导致误写。
LOG_FILE="${LOG_FILE:-/home/asus/projects/pharmacy-learning-platform/docs/audit-fix/fix-log.txt}"
log_line() {
  printf '%s | %s | %s | %s\n' "$(date '+%Y-%m-%d %H:%M')" "$1" "$2" "$3" >> "$LOG_FILE"
}
