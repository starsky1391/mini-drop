import { useState, useEffect, useCallback } from "react";
import { Outlet, useNavigate, useLocation } from "react-router-dom";
import { Button, Input, Layout, Menu, message, Space, Tag, Tooltip, Typography } from "antd";
import {
  DashboardOutlined,
  AuditOutlined,
  ExperimentOutlined,
  SettingOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  KeyOutlined,
  BulbOutlined,
  BulbFilled,
  ApiOutlined,
  WifiOutlined,
  RobotOutlined,
  RadarChartOutlined,
} from "@ant-design/icons";
import { getStoredApiKey, saveApiKey, createEventSource } from "../api/client";
import ErrorBoundary from "../components/ErrorBoundary";
import { COLORS, LAYOUT, SPACING, FONT_SIZES } from "../theme";

const { Sider, Header, Content } = Layout;

const MENU_ITEMS = [
  { key: "/", icon: <DashboardOutlined />, label: "任务面板" },
  { key: "/ai-diagnosis", icon: <RobotOutlined />, label: "AI 集群诊断" },
  { key: "/persistent-watch", icon: <RadarChartOutlined />, label: "持续监视" },
  { key: "/diagnoses", icon: <ExperimentOutlined />, label: "诊断历史" },
  { key: "/audit", icon: <AuditOutlined />, label: "审计日志" },
  { key: "/settings", icon: <SettingOutlined />, label: "系统设置" },
];

// ── 暗色主题 tokens ───────────────────────────────────────────

const DARK_TOKENS = {
  bgLayout: "#141414",
  bgContent: "#1f1f1f",
  bgHeader: "#1f1f1f",
  borderColor: "#303030",
  textPrimary: "rgba(255,255,255,0.85)",
  textSecondary: "rgba(255,255,255,0.65)",
  textTertiary: "rgba(255,255,255,0.45)",
  cardBg: "#1f1f1f",
};

const LIGHT_TOKENS = {
  bgLayout: "#f5f5f5",
  bgContent: COLORS.cardBackground,
  bgHeader: COLORS.cardBackground,
  borderColor: COLORS.border,
  textPrimary: COLORS.textPrimary,
  textSecondary: COLORS.textSecondary,
  textTertiary: COLORS.textTertiary,
  cardBg: COLORS.cardBackground,
};

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const [apiKey, setApiKey] = useState(getStoredApiKey() || "");
  const [darkMode, setDarkMode] = useState(() => {
    try {
      return localStorage.getItem("mini-drop-theme") === "dark";
    } catch {
      return false;
    }
  });

  // SSE 连接状态
  const [sseConnected, setSseConnected] = useState(false);

  const T = darkMode ? DARK_TOKENS : LIGHT_TOKENS;

  // ── 暗色模式持久化 ──────────────────────────────────────

  const toggleDarkMode = useCallback(() => {
    setDarkMode((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("mini-drop-theme", next ? "dark" : "light");
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  // ── SSE 事件流 ──────────────────────────────────────────

  useEffect(() => {
    const es = createEventSource();
    es.onopen = () => setSseConnected(true);
    es.onerror = () => setSseConnected(false);
    return () => es.close();
  }, []);

  // ── 路由激活 key ─────────────────────────────────────────

  const path = location.pathname;
  const selectedKey = MENU_ITEMS.find(
    (item) => path === item.key || (item.key !== "/" && path.startsWith(item.key))
  )?.key || "/";

  // ── 保存 API Key ─────────────────────────────────────────

  async function handleSaveKey() {
    await saveApiKey(apiKey.trim());
    message.success(
      apiKey.trim()
        ? "平台访问 Key 已保存 (HttpOnly Cookie + 降级)"
        : "平台访问 Key 已清除"
    );
  }

  return (
    <Layout
      style={{
        minHeight: "100vh",
        background: T.bgLayout,
        transition: "background 0.3s ease",
      }}
    >
      {/* ── 侧边栏 ─────────────────────────────────────────── */}
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={(v) => setCollapsed(v)}
        breakpoint="lg"
        collapsedWidth={64}
        width={LAYOUT.siderWidth}
        theme="dark"
        style={{
          overflow: "auto",
          height: "100vh",
          position: "sticky",
          top: 0,
          left: 0,
        }}
      >
        {/* Logo */}
        <div
          style={{
            height: LAYOUT.headerHeight,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            borderBottom: "1px solid rgba(255,255,255,0.12)",
            gap: collapsed ? 0 : 8,
          }}
        >
          <ApiOutlined
            style={{
              fontSize: collapsed ? 20 : 18,
              color: COLORS.primary,
              transition: "transform 0.3s",
            }}
          />
          {!collapsed && (
            <Typography.Text
              strong
              style={{
                color: "#fff",
                fontSize: 16,
                letterSpacing: 0.5,
                whiteSpace: "nowrap",
              }}
            >
              Mini-Drop
            </Typography.Text>
          )}
        </div>

        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={MENU_ITEMS}
          onClick={({ key }) => navigate(key)}
          style={{ marginTop: SPACING.sm }}
        />

        {/* SSE 指示器 */}
        <div
          style={{
            position: "absolute",
            bottom: 80,
            left: 0,
            right: 0,
            padding: "0 16px",
            textAlign: "center",
          }}
        >
          <Tooltip
            title={
              sseConnected ? "实时事件推送已连接" : "实时事件推送断开（轮询兜底）"
            }
          >
            <Tag
              icon={<WifiOutlined />}
              color={sseConnected ? "green" : "default"}
              style={{
                width: "100%",
                textAlign: "center",
                border: "none",
                background: sseConnected
                  ? "rgba(82,196,26,0.15)"
                  : "rgba(255,255,255,0.06)",
                color: sseConnected ? "#52c41a" : "rgba(255,255,255,0.3)",
                fontSize: 11,
              }}
            >
              {collapsed ? "" : sseConnected ? "SSE 已连接" : "SSE 断开"}
            </Tag>
          </Tooltip>
        </div>
      </Sider>

      {/* ── 主区域 ─────────────────────────────────────────── */}
      <Layout>
        {/* 顶栏 */}
        <Header
          style={{
            height: LAYOUT.headerHeight,
            lineHeight: `${LAYOUT.headerHeight}px`,
            padding: `0 ${SPACING.lg}px`,
            background: T.bgHeader,
            borderBottom: `1px solid ${T.borderColor}`,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            position: "sticky",
            top: 0,
            zIndex: 10,
            transition: "background 0.3s ease, border-color 0.3s ease",
          }}
        >
          <Space size="middle">
            <Typography.Text
              strong
              style={{
                fontSize: FONT_SIZES.lg,
                whiteSpace: "nowrap",
                color: T.textPrimary,
              }}
            >
              Mini-Drop 性能诊断平台
            </Typography.Text>
            <Tag
              color={sseConnected ? "green" : "default"}
              style={{ fontSize: 10, lineHeight: "16px" }}
            >
              {sseConnected ? "实时连接" : "轮询模式"}
            </Tag>
          </Space>

          <Space size="small" wrap style={{ flexShrink: 0 }}>
            {/* 暗色模式切换 */}
            <Tooltip title={darkMode ? "切换亮色模式" : "切换暗色模式"}>
              <Button
                size="small"
                type="text"
                icon={
                  darkMode ? (
                    <BulbFilled style={{ color: COLORS.warning }} />
                  ) : (
                    <BulbOutlined />
                  )
                }
                onClick={toggleDarkMode}
                style={{ color: T.textSecondary }}
              />
            </Tooltip>

            <Input.Password
              placeholder="平台访问 Key（启用鉴权时需要）"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              onPressEnter={handleSaveKey}
              size="small"
              style={{ width: 200, maxWidth: "40vw" }}
              prefix={<KeyOutlined style={{ color: T.textSecondary }} />}
            />
            <Button size="small" type="primary" onClick={handleSaveKey}>
              保存
            </Button>
          </Space>
        </Header>

        {/* 内容 */}
        <Content
          style={{
            margin: SPACING.lg,
            padding: SPACING.xl,
            background: T.bgContent,
            borderRadius: 8,
            minHeight: `calc(100vh - ${LAYOUT.headerHeight}px - ${SPACING.lg * 2}px)`,
            border: `1px solid ${T.borderColor}`,
            transition: "background 0.3s ease, border-color 0.3s ease",
          }}
        >
          <ErrorBoundary key={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  );
}
