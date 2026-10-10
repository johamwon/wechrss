## What changed

### 批量操作功能

- **后端方法**: 在 `AppDB` 类中添加 `delete_sources_batch()` 方法，支持批量删除多个公众号
- **Web 路由**: 添加 `/sources/batch/delete` POST 路由，处理批量删除请求
- **仪表板 UI**: 
  - 每个公众号前添加复选框用于选择
  - 添加批量操作工具栏，包含"全选"复选框和"批量删除"按钮
  - 删除前显示确认对话框防止误操作
- **JavaScript**: 实现全选/取消全选功能，支持 indeterminate 状态
- **样式**: 添加响应式 CSS，移动端自动调整布局

### 批量操作特性

- 支持一次删除多个公众号订阅
- 全选/取消全选快捷操作
- 半选状态显示（indeterminate）
- CSRF 保护确保安全
- 删除前确认提示
- 操作完成后显示删除数量反馈

### 文件变更

- `service.py`: 添加 `AppDB.delete_sources_batch()` 方法
- `web_app.py`: 添加 `/sources/batch/delete` 路由
- `templates/dashboard.html`: 添加批量选择 UI
- `static/app.css`: 添加批量操作样式
- `static/app.js`: 添加全选功能 JavaScript
- `tests/test_batch.py`: 新增 6 个测试

## Verification

- Tests: 45 passed, 1 failed (预存在的 Content-Type 测试失败，与本次变更无关)
- Build: pass
- 新增 6 个测试覆盖批量操作的各种场景
