# 联采密封报价与授标签约

联合采购机构按公告的窗口和预算接收采购报价。资料中供应方、报价和争议是模拟单据，金额单位为分。

业务记录使用 [《联采报价单据》](docs/exchange.md) 的格式。`data/` 中含各参与方合成单据，可由 Python 3.11.2 读取，无需第三方包。SQLite 来自 Python 自带模块。

服务实现见 [`liancai/`](liancai/)，业务规则与接口说明见 [实现说明](docs/implementation.md)。

## 快速开始

```sh
python3 -m liancai --db demo.sqlite3 init        # 建库、身份、凭证、公告
python3 -m liancai --db demo.sqlite3 seed-demo --evaluate
python3 -m liancai --db demo.sqlite3 serve       # JSON 接口，默认 127.0.0.1:8412
```

## 交换资料

在项目根目录启动本地资料源，可按 JSON 文件名访问记录。

```sh
python3 -m http.server --bind 127.0.0.1 --directory data 8411
```

## 资料校验与测试

```sh
python3 -m unittest discover -s tests -v
```
