# 联采密封报价与授标签约

联合采购机构按公告的窗口和预算接收采购报价。资料中供应方、报价和争议是模拟单据，金额单位为分。

业务记录使用 [《联采报价单据》](docs/exchange.md) 的格式。`data/` 中含各参与方合成单据，可由 Python 3.11.2 读取，无需第三方包。SQLite 来自 Python 自带模块。

## 交换资料

在项目根目录启动本地资料源，可按 JSON 文件名访问记录。

```sh
python3 -m http.server --bind 127.0.0.1 --directory data 8411
```

## 资料校验

```sh
python3 -m unittest discover -s tests -v
```
