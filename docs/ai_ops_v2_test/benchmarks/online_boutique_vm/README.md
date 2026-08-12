# Online Boutique VM 验证

该目录用于三节点 Hyper-V 环境中的真实运行验证：Control 位于 `172.18.88.237`，两个 Worker 位于 `172.18.90.144` 和 `172.18.87.120`。

## 部署

两个 Worker 需预先导入以下本地镜像：

- `online-boutique/<service>:v0.8.0`，共 11 个服务；
- `redis:alpine`。

在 worker1 初始化 Swarm、让 worker2 作为 Worker 加入后执行：

```bash
sudo docker stack deploy --resolve-image never \
  -c stack.yml boutique
```

入口为 `http://172.18.90.144:8080`。Swarm routing mesh 也允许通过 worker2 的 8080 端口访问。

## 分布

- worker1：frontend、productcatalogservice、currencyservice、recommendationservice、loadgenerator；
- worker2：cartservice、redis-cart、checkoutservice、paymentservice、shippingservice、emailservice、adservice。

这种分布会让首页、购物车和结算产生真实跨 Worker 调用。

## 故障规则

故障定义见 `scenarios.json`。每个案例至少执行两轮，每轮必须包含基线、注入、故障观测、回滚和恢复探测。

- 网络规则必须先建立自动回滚定时器，再写入 iptables；
- I/O 和内存压力必须使用带 `RuntimeMaxSec` 的 transient systemd unit；
- 磁盘耗尽只允许在 loopback 测试文件系统中执行，不写满 VM 根盘；
- PID 和 container ID 每轮从 Docker 实时解析，不使用固定值；
- Mini-Drop 诊断上下文必须提交 `service_id`、`instance_id`、`host_id`、`agent_id`、PID、container ID 和服务关系。

VM 密码只通过 `MINI_DROP_VM_PASSWORD` 环境变量传入，不写入仓库和报告。
