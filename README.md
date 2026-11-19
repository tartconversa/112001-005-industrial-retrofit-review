# 工业改造基准评估

用于分期评估企业改造的合成生产边界、计量、产量与改造记录。

## 运行环境

需要 Python 3.11 或更新版本。项目仅使用 Python 标准库。

## 本地验证

在项目目录运行 `python -m unittest discover -s tests -v`，检查随附 CSV 样例的结构。

## 构建检查

运行 `python -m compileall -q tests` 检查测试文件语法。

## 数据说明

`data/` 目录包含少量合成输入记录，字段范围见其中的说明文件。
