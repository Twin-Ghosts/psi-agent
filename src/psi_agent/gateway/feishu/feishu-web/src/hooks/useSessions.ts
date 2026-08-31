import { useCallback, useEffect, useRef, useState } from "react";
import {
  createSession,
  deleteSession,
  getSessionHistory,
  listAis,
  listSessions,
  listTitles,
  setTitle,
  type SessionInfo,
} from "../api";

/**
 * 会话列表 + 标题 + 当前选中会话。
 *
 * 从 PR 的 App.tsx 里拆出来的原因: 那边把会话列表、历史加载、流式收发、任务过滤全塞在
 * 一个 829 行的组件里, 状态互相串。这里一个 hook 只管一件事, 流式收发在 useChatTurn。
 */
export function useSessions() {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [titles, setTitles] = useState<Record<string, string>>({});
  const [currentId, setCurrentId] = useState<string>("");
  const [defaultAiId, setDefaultAiId] = useState<string>("");
  const [error, setError] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      const [list, titleMap] = await Promise.all([listSessions(), listTitles()]);
      setSessions(list);
      setTitles(titleMap);
      return list;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return [];
    }
  }, []);

  // 首屏: 会话列表 + 可用模型。模型只用于新建会话时挑 backend_id。
  useEffect(() => {
    void (async () => {
      await refresh();
      try {
        const ais = await listAis();
        if (ais.length) setDefaultAiId(ais[0].id);
      } catch {
        // 模型列表拿不到不该挡住会话列表, 新建会话时再报。
      }
    })();
  }, [refresh]);

  const create = useCallback(async () => {
    if (!defaultAiId) {
      setError("没有可用模型, 无法新建会话");
      return "";
    }
    try {
      // 不传 id → 后端发新 uuid → 新 jsonl。这是「网页里能开多个会话」的全部机制。
      const info = await createSession(defaultAiId);
      // 先落一个占位标题: 首轮结束后 App.tsx 会用首句 prompt 派生的标题覆盖它。没有占位
      // 的话列表里会是一排「未命名任务」, 多会话反而更难用。
      const placeholder = `新会话 ${new Date().toLocaleString("zh-CN", { hour12: false })}`;
      await setTitle(info.id, placeholder).catch(() => undefined);
      setTitles((prev) => ({ ...prev, [info.id]: placeholder }));
      await refresh();
      setCurrentId(info.id);
      return info.id;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return "";
    }
  }, [defaultAiId, refresh]);

  const remove = useCallback(
    async (id: string) => {
      try {
        await deleteSession(id);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        return;
      }
      const list = await refresh();
      setCurrentId((cur) => (cur === id ? list[0]?.id || "" : cur));
    },
    [refresh],
  );

  return {
    sessions,
    titles,
    currentId,
    setCurrentId,
    defaultAiId,
    error,
    setError,
    refresh,
    create,
    remove,
    setTitles,
  };
}

/** 按会话 id 拉历史, 切换会话时自动取消上一次 (避免旧响应盖掉新会话)。 */
export function useSessionHistory(sessionId: string) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [raw, setRaw] = useState<Awaited<ReturnType<typeof getSessionHistory>>>([]);
  const seq = useRef(0);

  const reload = useCallback(async (id: string) => {
    const mine = ++seq.current;
    if (!id) {
      setRaw([]);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const data = await getSessionHistory(id);
      if (seq.current !== mine) return; // 已切走, 丢弃
      setRaw(data);
    } catch (err) {
      if (seq.current !== mine) return;
      setError(err instanceof Error ? err.message : String(err));
      setRaw([]);
    } finally {
      if (seq.current === mine) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload(sessionId);
  }, [sessionId, reload]);

  return { raw, setRaw, loading, error, reload };
}
