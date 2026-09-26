# 个人账单分析工具

本地运行的 Windows 账单分析工具。把微信、支付宝、中国银行三份账单导入后，自动合并重复记录、自动分类，并给出本月花了多少、花在哪。

## 直接运行（开发/未打包时）

环境要求：Python 3.10+。

```powershell
cd 当前目录
py -m pip install -r requirements.txt
py run.py
```

启动后会自动弹出本地窗口；如果本机没有安装 `pywebview`，会改用系统默认浏览器打开。

## 打包成单个 exe

```powershell
cd 当前目录
.\build_exe.bat
```

完成后，单文件程序在：

```text
output\dist\个人账单分析工具.exe
```

## 数据位置

数据库只保存在本机：

```text
%LOCALAPPDATA%\BillAnalyzer\bill_data.db
```

程序不联网、不上传、不调用外部 API。前端也没有任何 CDN 引用。

## 账单格式

- 微信：导出 CSV，UTF-8。表头以「交易时间」开头，前面说明行会自动跳过。
- 支付宝：导出 CSV，GBK/GB18030。会自动识别编码，避免乱码。
- 中国银行：把手机银行流水复制到 Excel 后另存为 `.xlsx`（或 `.xls`），保留 11 列表头。

导入同一份文件重复时，已存在记录会自动跳过，并在结果中显示跳过条数。

## 文件夹说明

- `app.py`、`run.py`、`templates/`：程序源码。
- `packaging/`：PyInstaller 打包配置。
- `output/`：打包生成文件和本地截图；不会提交到 Git。
