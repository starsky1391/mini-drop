import { useEffect, useState, useCallback } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Row,
  Skeleton,
  Space,
  Select,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  SettingOutlined,
  RobotOutlined,
  SafetyOutlined,
  CloudServerOutlined,
  ReloadOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
} from "@ant-design/icons";
import {
  healthz,
  getAIConfig,
  getStoredApiKey,
  saveApiKey,
  activateAIProviderProfile,
  createAIProviderProfile,
  deleteAIProviderProfile,
  listAIProviderProfiles,
  testAIProviderProfile,
  testActiveAIConfig,
  testSavedAIProviderProfile,
  updateAIProviderProfile,
} from "../api/client";
import ErrorAlert from "../components/ErrorAlert";
import { COLORS, FONT_SIZES, SPACING } from "../theme";

export default function Settings() {
  const [profileForm] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [health, setHealth] = useState(null);
  const [aiConfig, setAiConfig] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const [apiKey, setApiKey] = useState(getStoredApiKey() || "");
  const [savingKey, setSavingKey] = useState(false);
  const [profileModalOpen, setProfileModalOpen] = useState(false);
  const [editingProfile, setEditingProfile] = useState(null);
  const [savingProfile, setSavingProfile] = useState(false);
  const [testingProfileId, setTestingProfileId] = useState("");
  const [testingActiveConfig, setTestingActiveConfig] = useState(false);

  const load = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      const results = await Promise.allSettled([
        healthz(),
        getAIConfig().catch(() => null),
        listAIProviderProfiles().catch(() => []),
      ]);
      if (results[0].status === "fulfilled") setHealth(results[0].value);
      if (results[1].status === "fulfilled") setAiConfig(results[1].value);
      if (results[2].status === "fulfilled") setProfiles(results[2].value);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const checks = health?.checks || {};
  const featureStatus = (enabled) =>
    enabled ? (
      <Tag icon={<CheckCircleOutlined />} color="green">已启用</Tag>
    ) : (
      <Tag icon={<CloseCircleOutlined />} color="default">已禁用</Tag>
    );
  const profileColumns = [
    {
      title: "名称",
      dataIndex: "name",
      render: (value, record) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <Typography.Text strong>{value}</Typography.Text>
            {record.is_active && <Tag color="green">active</Tag>}
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
            {record.provider_label}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "模型",
      dataIndex: "model",
      width: 180,
      render: (value) => <Tag>{value}</Tag>,
    },
    {
      title: "端点",
      dataIndex: "base_url",
      render: (value) => (
        <Typography.Text copyable ellipsis style={{ maxWidth: 280 }}>
          {value}
        </Typography.Text>
      ),
    },
    {
      title: "Key",
      width: 120,
      render: (_, record) => (
        <Tag color={record.has_api_key ? "green" : "red"}>
          {record.has_api_key ? record.api_key_hint || "已配置" : "未配置"}
        </Tag>
      ),
    },
    {
      title: "模式",
      dataIndex: "enabled",
      width: 110,
      render: (value) => <Tag color={value === "full" ? "purple" : "default"}>{value}</Tag>,
    },
    {
      title: "操作",
      width: 270,
      render: (_, record) => (
        <Space size={4} wrap>
          <Button size="small" onClick={() => openEditProfile(record)}>
            编辑
          </Button>
          <Button
            size="small"
            disabled={record.is_active}
            onClick={() => handleActivateProfile(record)}
          >
            激活
          </Button>
          <Button
            size="small"
            loading={testingProfileId === record.profile_id}
            disabled={!record.is_active}
            onClick={() => handleTestSavedProfile(record)}
          >
            测试
          </Button>
          <Popconfirm
            title="删除这个 AI Provider Profile？"
            okText="删除"
            cancelText="取消"
            onConfirm={() => handleDeleteProfile(record)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  async function handleSaveKey() {
    setSavingKey(true);
    try {
      await saveApiKey(apiKey.trim());
      setApiKey(apiKey.trim());
      message.success(apiKey.trim() ? "平台访问 Key 已保存" : "平台访问 Key 已清除");
    } catch (err) {
      message.error(err.message);
    } finally {
      setSavingKey(false);
    }
  }

  function openCreateProfile() {
    setEditingProfile(null);
    profileForm.resetFields();
    profileForm.setFieldsValue({
      provider_label: "openai-compatible",
      base_url: "https://api.deepseek.com",
      enabled: "full",
      activate: true,
    });
    setProfileModalOpen(true);
  }

  function openEditProfile(profile) {
    setEditingProfile(profile);
    profileForm.resetFields();
    profileForm.setFieldsValue({
      name: profile.name,
      provider_label: profile.provider_label,
      base_url: profile.base_url,
      model: profile.model,
      enabled: profile.enabled,
      api_key: "",
      activate: profile.is_active,
    });
    setProfileModalOpen(true);
  }

  async function handleSaveProfile() {
    const values = await profileForm.validateFields();
    setSavingProfile(true);
    try {
      const payload = { ...values, base_url: values.base_url?.trim(), model: values.model?.trim() };
      if (editingProfile) {
        delete payload.activate;
        if (!payload.api_key) delete payload.api_key;
        await updateAIProviderProfile(editingProfile.profile_id, payload);
        message.success("AI Provider Profile 已更新");
      } else {
        await createAIProviderProfile(payload);
        message.success("AI Provider Profile 已创建");
      }
      setProfileModalOpen(false);
      load();
    } catch (err) {
      message.error(err.message);
    } finally {
      setSavingProfile(false);
    }
  }

  async function handleTestProfileForm() {
    const values = await profileForm.validateFields();
    setTestingProfileId("__form__");
    try {
      const result = await testAIProviderProfile(values);
      if (result.passed) {
        message.success(`Provider 测试通过，耗时 ${result.duration_ms} ms`);
      } else {
        message.error(`Provider 测试失败（HTTP ${result.http_status || "N/A"}）`);
      }
    } catch (err) {
      message.error(err.message);
    } finally {
      setTestingProfileId("");
    }
  }

  async function handleActivateProfile(profile) {
    await activateAIProviderProfile(profile.profile_id);
    message.success("已切换 active AI Provider Profile");
    load();
  }

  async function handleDeleteProfile(profile) {
    await deleteAIProviderProfile(profile.profile_id);
    message.success("AI Provider Profile 已删除");
    load();
  }

  async function handleTestSavedProfile(profile) {
    setTestingProfileId(profile.profile_id);
    try {
      const result = await testSavedAIProviderProfile(profile.profile_id);
      if (result.passed) {
        message.success(`Provider 测试通过，耗时 ${result.duration_ms} ms`);
      } else {
        message.warning(result.message || `Provider 测试失败（HTTP ${result.http_status || "N/A"}）`);
      }
    } catch (err) {
      message.error(err.message);
    } finally {
      setTestingProfileId("");
    }
  }

  async function handleTestActiveConfig() {
    setTestingActiveConfig(true);
    try {
      const result = await testActiveAIConfig();
      if (result.passed) {
        message.success(
          `当前 AI 配置连接成功：${result.provider || "provider"} / ${result.model || "model"}`
        );
      } else {
        message.error(
          result.message || `当前 AI 配置连接失败（HTTP ${result.http_status || "N/A"}）`
        );
      }
    } catch (err) {
      message.error(err.message);
    } finally {
      setTestingActiveConfig(false);
    }
  }

  if (loading) {
    return (
      <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
        <Skeleton.Input active size="small" style={{ width: 160 }} />
        {[1, 2, 3].map((i) => (
          <Card key={i} size="small">
            <Skeleton active paragraph={{ rows: 5 }} />
          </Card>
        ))}
      </Space>
    );
  }

  return (
    <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
      {/* 页头 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: 8,
        }}
      >
        <Space align="center">
          <SettingOutlined style={{ fontSize: 20, color: COLORS.primary }} />
          <Typography.Title level={4} style={{ margin: 0 }}>
            系统设置
          </Typography.Title>
        </Space>
        <Button icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
      </div>

      <ErrorAlert error={error} onClose={() => setError("")} />

      {/* 服务健康 */}
      <Card
        title={
          <Space>
            <CloudServerOutlined style={{ color: COLORS.primary }} />
            服务健康
          </Space>
        }
        size="small"
        extra={
          <Tag color={health?.healthy ? "green" : "red"}>
            {health?.healthy ? "健康" : "异常"}
          </Tag>
        }
      >
        <Descriptions column={{ xs: 1, sm: 2 }} size="small" bordered>
          <Descriptions.Item label="服务名">
            {health?.service || "mini-drop-server"}
          </Descriptions.Item>
          <Descriptions.Item label="版本">
            <Tag>{health?.version || "0.1.0"}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="数据库">
            {checks.database ? (
              <Tag color={checks.database.status === "ok" ? "green" : "red"}>
                {checks.database.status === "ok" ? "✓ 连通" : "✗ 不可用"}
              </Tag>
            ) : (
              <Tag>未知</Tag>
            )}
          </Descriptions.Item>
          <Descriptions.Item label="对象存储">
            {checks.storage ? (
              <Tag color={checks.storage.status === "ok" ? "green" : "red"}>
                {checks.storage.status === "ok" ? "✓ 连通" : "✗ 不可用"}
              </Tag>
            ) : (
              <Tag>未知</Tag>
            )}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* AI 配置 */}
      <Card
        title={
          <Space>
            <RobotOutlined style={{ color: COLORS.warning }} />
            AI Provider 配置
          </Space>
        }
        size="small"
        extra={
          <Space size={8}>
            {aiConfig?.enabled && aiConfig.enabled !== "none" ? (
              <Tag color="orange">AI: {aiConfig.enabled}</Tag>
            ) : (
              <Tag>AI 未启用</Tag>
            )}
            <Button
              size="small"
              loading={testingActiveConfig}
              onClick={handleTestActiveConfig}
            >
              测试当前配置
            </Button>
          </Space>
        }
      >
        {aiConfig ? (
          <Descriptions column={{ xs: 1, sm: 2, md: 3 }} size="small" bordered>
            <Descriptions.Item label="厂商">
              <Tag color="blue">{aiConfig.provider || "unknown"}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="来源">
              <Tag color={aiConfig.source === "profile" ? "green" : "default"}>
                {aiConfig.source === "profile" ? "页面 Profile" : "环境变量"}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="模型">
              <Tag>{aiConfig.model || "N/A"}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="API 端点">
              <Typography.Text
                copyable
                ellipsis
                style={{ maxWidth: 240, fontSize: FONT_SIZES.sm }}
              >
                {aiConfig.base_url || "N/A"}
              </Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="API Key">
              <Tag color={aiConfig.has_api_key ? "green" : "red"}>
                {aiConfig.has_api_key ? "已配置" : "未配置"}
              </Tag>
            </Descriptions.Item>
            {aiConfig.profile_id && (
              <Descriptions.Item label="Active Profile">
                <Typography.Text code>{aiConfig.profile_id}</Typography.Text>
              </Descriptions.Item>
            )}
            <Descriptions.Item label="策略模式">
              <Tag color="purple">{aiConfig.enabled || "none"}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="功能开关" span={2}>
              <Space wrap>
                {featureStatus(aiConfig.features?.nlp)}
                <Typography.Text style={{ fontSize: FONT_SIZES.sm }}>NLP 自然语言</Typography.Text>
                {featureStatus(aiConfig.features?.rca)}
                <Typography.Text style={{ fontSize: FONT_SIZES.sm }}>RCA 智能归因</Typography.Text>
                {featureStatus(aiConfig.features?.summarize)}
                <Typography.Text style={{ fontSize: FONT_SIZES.sm }}>AI 总结</Typography.Text>
              </Space>
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <Alert
            type="warning"
            message="无法获取 AI 配置"
            description="请确认已设置 active AI Provider Profile 或 MINI_DROP_AI_ENABLED 等环境变量"
            showIcon
          />
        )}
      </Card>

      {/* AI Provider Profiles */}
      <Card
        title={
          <Space>
            <RobotOutlined style={{ color: COLORS.warning }} />
            AI Provider Profiles
          </Space>
        }
        size="small"
        extra={
          <Button type="primary" size="small" onClick={openCreateProfile}>
            新建 Provider
          </Button>
        }
      >
        <Space direction="vertical" style={{ width: "100%" }} size={12}>
          <Alert
            type="info"
            message="这里保存的是模型供应商 Key，不是 Mini-Drop 控制面访问 Key"
            description="所有配置按 OpenAI-compatible Chat Completions 格式调用；服务端只返回脱敏信息，不会回显完整 API Key。"
            showIcon
          />
          <Table
            size="small"
            rowKey="profile_id"
            columns={profileColumns}
            dataSource={profiles}
            pagination={false}
            scroll={{ x: 920 }}
            locale={{ emptyText: "暂无 Profile，系统会继续使用环境变量 fallback" }}
          />
        </Space>
      </Card>

      {/* API 认证 */}
      <Card
        title={
          <Space>
            <SafetyOutlined style={{ color: COLORS.primary }} />
            平台访问控制
          </Space>
        }
        size="small"
        extra={
          aiConfig?.control_api_auth?.enabled ? (
            apiKey ? (
              <Tag color="green">访问 Key 已设置</Tag>
            ) : (
              <Tag color="red">访问 Key 未设置</Tag>
            )
          ) : (
            <Tag color="default">鉴权未启用</Tag>
          )
        }
      >
        <Space direction="vertical" style={{ width: "100%" }} size={12}>
          <Alert
            type={aiConfig?.control_api_auth?.enabled ? "warning" : "info"}
            message={
              aiConfig?.control_api_auth?.enabled
                ? "控制面 API 鉴权已启用"
                : "控制面 API 鉴权未启用，开发环境无需填写平台访问 Key"
            }
            description="这个 Key 只用于访问 Mini-Drop 控制面 API，不会作为 AI Provider Key 使用。"
            showIcon
          />
          <Input.Password
            placeholder="Mini-Drop 平台访问 Key（仅控制面鉴权启用时需要）"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            onPressEnter={handleSaveKey}
            allowClear
          />
          <Space size={8}>
            <Button
              type="primary"
              size="small"
              loading={savingKey}
              onClick={handleSaveKey}
            >
              保存访问 Key
            </Button>
            {apiKey && (
              <Button
                size="small"
                danger
                onClick={async () => {
                  setApiKey("");
                  await saveApiKey("");
                  message.success("平台访问 Key 已清除");
                }}
              >
                清除访问 Key
              </Button>
            )}
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
            清除后会同时清理 HttpOnly Cookie 和 localStorage 降级值。
          </Typography.Text>
        </Space>
      </Card>

      <Modal
        title={editingProfile ? "编辑 AI Provider Profile" : "新建 AI Provider Profile"}
        open={profileModalOpen}
        onCancel={() => setProfileModalOpen(false)}
        onOk={handleSaveProfile}
        confirmLoading={savingProfile}
        okText="保存"
        cancelText="取消"
        width={720}
        footer={(_, { OkBtn, CancelBtn }) => (
          <Space style={{ width: "100%", justifyContent: "space-between" }}>
            <Button
              loading={testingProfileId === "__form__"}
              onClick={handleTestProfileForm}
            >
              测试当前表单
            </Button>
            <Space>
              <CancelBtn />
              <OkBtn />
            </Space>
          </Space>
        )}
      >
        <Form
          form={profileForm}
          layout="vertical"
          requiredMark={false}
          style={{ marginTop: 16 }}
        >
          <Row gutter={12}>
            <Col xs={24} md={12}>
              <Form.Item
                name="name"
                label="Profile 名称"
                rules={[{ required: true, message: "请输入 Profile 名称" }]}
              >
                <Input placeholder="例如：DeepSeek 官方 / 本地代理" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="provider_label"
                label="供应商标签"
                rules={[{ required: true, message: "请输入供应商标签" }]}
              >
                <Input placeholder="openai-compatible" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="base_url"
            label="Base URL"
            rules={[{ required: true, message: "请输入 OpenAI-compatible Base URL" }]}
          >
            <Input placeholder="https://api.deepseek.com 或 http://127.0.0.1:8787/v1" />
          </Form.Item>
          <Row gutter={12}>
            <Col xs={24} md={12}>
              <Form.Item
                name="model"
                label="模型"
                rules={[{ required: true, message: "请输入模型名" }]}
              >
                <Input placeholder="deepseek-chat / gpt-4.1-mini / ..." />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="enabled"
                label="启用模式"
                rules={[{ required: true, message: "请选择启用模式" }]}
              >
                <Select
                  options={[
                    { value: "full", label: "full：NLP + RCA + 总结" },
                    { value: "rca-only", label: "rca-only：只启用归因" },
                    { value: "nlp-only", label: "nlp-only：只启用自然语言" },
                    { value: "none", label: "none：保存但禁用" },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="api_key"
            label={editingProfile ? "API Key（留空表示不修改）" : "API Key"}
            rules={editingProfile ? [] : [{ required: true, message: "请输入 AI Provider API Key" }]}
          >
            <Input.Password placeholder="仅服务端保存，页面不会回显完整 Key" />
          </Form.Item>
          {!editingProfile && (
            <Form.Item name="activate" label="激活策略">
              <Select
                options={[
                  { value: true, label: "创建后立即激活" },
                  { value: false, label: "仅保存，不激活" },
                ]}
              />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </Space>
  );
}
