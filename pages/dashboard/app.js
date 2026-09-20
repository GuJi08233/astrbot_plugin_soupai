/* 海龟汤管理页。
 *
 * 页面跑在 AstrBot 面板的 iframe 里，自身没有登录态，所以不能直接 fetch 后端，
 * 要通过 postMessage 让父窗口代发请求。桥只提供 api:get / api:post。
 *
 * 汤底永远单独请求、且需要点一下才显示 —— 管理员翻题库时不该被剧透。
 */
(() => {
  'use strict';

  const CHANNEL = 'astrbot-plugin-page';
  const pending = new Map();
  let seq = 0;

  window.addEventListener('message', (event) => {
    const msg = event.data;
    if (!msg || msg.channel !== CHANNEL || msg.kind !== 'response') return;
    const slot = pending.get(msg.requestId);
    if (!slot) return;
    pending.delete(msg.requestId);
    msg.ok ? slot.resolve(msg.data) : slot.reject(new Error(msg.error || '请求失败'));
  });

  function bridge(action, payload) {
    return new Promise((resolve, reject) => {
      const requestId = `soupai-${Date.now()}-${++seq}`;
      pending.set(requestId, { resolve, reject });
      parent.postMessage({ channel: CHANNEL, requestId, action, ...payload }, '*');
      setTimeout(() => {
        if (pending.has(requestId)) {
          pending.delete(requestId);
          reject(new Error('请求超时'));
        }
      }, 120000);
    });
  }

  const api = {
    get: (endpoint, params) => bridge('api:get', { endpoint, params: params || {} }),
    post: (endpoint, body) => bridge('api:post', { endpoint, body: body || {} }),
  };

  // ─────────────────────────────────────────── 状态

  const state = {
    source: 'network',
    page: 1,
    pageSize: 20,
    total: 0,
    keyword: '',
    searchAnswer: false,
    session: '',
    sources: [],
    sessions: [],
    editing: null, // null=新增
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  let toastTimer;
  function toast(message, kind = 'info') {
    const box = $('#toast');
    box.textContent = message;
    box.className = `toast toast-${kind}`;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 3200);
  }

  async function guard(promise, okMessage) {
    try {
      const data = await promise;
      if (okMessage) toast(okMessage, 'ok');
      return data;
    } catch (err) {
      toast(err.message || String(err), 'error');
      throw err;
    }
  }

  // ─────────────────────────────────────────── 题库

  async function loadOverview() {
    const data = await api.get('overview', state.session ? { session: state.session } : {});
    state.sources = data.sources || [];
    state.sessions = data.sessions || [];
    renderStats(data);
    renderSourceSeg();
    renderSessionFilter();
    return data;
  }

  function renderStats(data) {
    const row = $('#statRow');
    row.innerHTML = '';
    state.sources.forEach((s) => {
      const card = el('div', 'stat');
      card.appendChild(el('div', 'stat-label', s.label + (s.readonly ? ' · 只读' : '')));
      card.appendChild(el('div', 'stat-value', String(s.total)));
      const sub = state.session
        ? `本会话已出 ${s.used}`
        : `所有会话共出过 ${s.used_all_sessions}`;
      card.appendChild(el('div', 'stat-sub', s.hidden ? `${sub} · 屏蔽 ${s.hidden}` : sub));
      row.appendChild(card);
    });
    const card = el('div', 'stat');
    card.appendChild(el('div', 'stat-label', '进行中对局'));
    card.appendChild(el('div', 'stat-value', String(data.active_games || 0)));
    card.appendChild(el('div', 'stat-sub', `判定引擎 ${data.judge_engine || 'llm'}`));
    row.appendChild(card);
  }

  function renderSourceSeg() {
    const seg = $('#sourceSeg');
    seg.innerHTML = '';
    state.sources.forEach((s) => {
      const btn = el('button', 'seg-item' + (s.source === state.source ? ' is-active' : ''), s.label);
      btn.onclick = () => {
        state.source = s.source;
        state.page = 1;
        renderSourceSeg();
        loadStories();
      };
      seg.appendChild(btn);
    });
  }

  function renderSessionFilter() {
    const sel = $('#sessionFilter');
    const prev = state.session;
    sel.innerHTML = '';
    sel.appendChild(new Option('全部会话', ''));
    state.sessions.forEach((s) => {
      sel.appendChild(new Option(`${s.label}（${s.used}）`, s.session));
    });
    sel.value = prev;
  }

  function currentSource() {
    return state.sources.find((s) => s.source === state.source) || {};
  }

  async function loadStories() {
    const params = {
      source: state.source,
      page: state.page,
      page_size: state.pageSize,
    };
    if (state.keyword) params.q = state.keyword;
    if (state.searchAnswer) params.search_answer = '1';
    if (state.session) params.session = state.session;

    const data = await guard(api.get('stories', params));
    state.total = data.total;
    renderList(data);
  }

  function renderList(data) {
    const list = $('#list');
    list.innerHTML = '';
    if (!data.items.length) {
      list.appendChild(el('div', 'empty', state.keyword ? '没有匹配的题目' : '这个题库还是空的'));
    }
    data.items.forEach((item) => list.appendChild(renderStory(item, data.readonly)));

    const pages = Math.max(Math.ceil(data.total / data.page_size), 1);
    $('#pageInfo').textContent = `第 ${data.page} / ${pages} 页 · 共 ${data.total} 题`;
    $('#prev').disabled = data.page <= 1;
    $('#next').disabled = data.page >= pages;
  }

  function renderStory(item, readonly) {
    const card = el('div', 'card' + (item.hidden ? ' is-hidden' : ''));

    const head = el('div', 'card-head');
    const tags = el('div', 'tags');
    if (item.used) tags.appendChild(el('span', 'tag tag-used', state.session ? '本会话已出' : '出过'));
    if (item.hidden) tags.appendChild(el('span', 'tag tag-muted', '已屏蔽'));
    head.appendChild(tags);
    head.appendChild(el('span', 'card-id', `#${item.index}`));
    card.appendChild(head);

    card.appendChild(el('p', 'puzzle', item.puzzle));

    // 汤底默认折叠，点一次才拉取
    const answerBox = el('div', 'answer');
    const reveal = el('button', 'btn btn-ghost btn-sm', `查看汤底（${item.answer_preview_len} 字）`);
    reveal.onclick = async () => {
      reveal.disabled = true;
      reveal.textContent = '加载中…';
      try {
        const data = await api.get('story/answer', { source: state.source, id: item.id });
        answerBox.innerHTML = '';
        answerBox.appendChild(el('p', 'answer-text', data.answer));
        const hide = el('button', 'btn btn-ghost btn-sm', '收起');
        hide.onclick = () => renderAnswerCollapsed();
        answerBox.appendChild(hide);
      } catch (err) {
        toast(err.message, 'error');
        renderAnswerCollapsed();
      }
    };
    function renderAnswerCollapsed() {
      answerBox.innerHTML = '';
      const btn = el('button', 'btn btn-ghost btn-sm', `查看汤底（${item.answer_preview_len} 字）`);
      btn.onclick = reveal.onclick;
      answerBox.appendChild(btn);
    }
    answerBox.appendChild(reveal);
    card.appendChild(answerBox);

    const actions = el('div', 'card-actions');
    if (!readonly) {
      const edit = el('button', 'btn btn-sm', '编辑');
      edit.onclick = () => openEditor(item);
      actions.appendChild(edit);

      const del = el('button', 'btn btn-sm btn-danger', '删除');
      del.onclick = async () => {
        if (!confirm(`删除这道题？\n\n${item.puzzle.slice(0, 60)}`)) return;
        await guard(api.post('story/delete', { source: state.source, id: item.id }), '已删除');
        await loadOverview();
        await loadStories();
      };
      actions.appendChild(del);
    }

    const hideBtn = el('button', 'btn btn-sm', item.hidden ? '取消屏蔽' : '屏蔽');
    hideBtn.title = '屏蔽后不会再被抽到，但题目仍保留';
    hideBtn.onclick = async () => {
      await guard(
        api.post('story/hide', { source: state.source, id: item.id, hidden: !item.hidden }),
        item.hidden ? '已取消屏蔽' : '已屏蔽',
      );
      await loadOverview();
      await loadStories();
    };
    actions.appendChild(hideBtn);

    if (state.session) {
      const mark = el('button', 'btn btn-sm', item.used ? '标为未出' : '标为已出');
      mark.onclick = async () => {
        await guard(
          api.post('usage/mark', {
            source: state.source, id: item.id, session: state.session, used: !item.used,
          }),
          '已更新',
        );
        await loadOverview();
        await loadStories();
      };
      actions.appendChild(mark);
    }

    card.appendChild(actions);
    return card;
  }

  // ─────────────────────────────────────────── 会话

  async function loadSessions() {
    const data = await guard(api.get('overview', {}));
    state.sessions = data.sessions || [];
    const list = $('#sessionList');
    list.innerHTML = '';
    if (!state.sessions.length) {
      list.appendChild(el('div', 'empty', '还没有任何会话出过题'));
      return;
    }
    state.sessions.forEach((s) => {
      const card = el('div', 'card');
      const head = el('div', 'card-head');
      head.appendChild(el('strong', null, s.label));
      if (s.playing) head.appendChild(el('span', 'tag tag-live', '进行中'));
      card.appendChild(head);
      card.appendChild(el('p', 'muted mono', s.session));
      card.appendChild(el('p', null, `已出过 ${s.used} 题`));

      const actions = el('div', 'card-actions');
      const view = el('button', 'btn btn-sm', '在题库中查看');
      view.onclick = () => {
        state.session = s.session;
        state.page = 1;
        switchTab('stories');
        renderSessionFilter();
        loadOverview().then(loadStories);
      };
      actions.appendChild(view);

      const reset = el('button', 'btn btn-sm btn-danger', '重置该会话');
      reset.onclick = async () => {
        if (!confirm(`重置「${s.label}」的出题记录？\n这些题在该会话中会重新可用。`)) return;
        await guard(api.post('usage/reset', { session: s.session }), '已重置');
        loadSessions();
      };
      actions.appendChild(reset);
      card.appendChild(actions);
      list.appendChild(card);
    });
  }

  // ─────────────────────────────────────────── 对局

  async function loadGames() {
    const data = await guard(api.get('games', {}));
    const list = $('#gameList');
    list.innerHTML = '';
    if (!data.games.length) {
      list.appendChild(el('div', 'empty', '当前没有进行中的对局'));
      return;
    }
    data.games.forEach((g) => {
      const card = el('div', 'card');
      const head = el('div', 'card-head');
      head.appendChild(el('strong', null, g.label));
      head.appendChild(el('span', 'tag', g.difficulty));
      card.appendChild(head);
      card.appendChild(el('p', 'puzzle', g.puzzle));

      const q = g.question_limit ? `${g.question_count}/${g.question_limit}` : `${g.question_count}（不限）`;
      const h = g.hint_limit ? `${g.hint_count}/${g.hint_limit}` : '不可用';
      const v = data.verification_limit > 0
        ? `${g.verification_attempts}/${data.verification_limit}` : `${g.verification_attempts}（不限）`;
      card.appendChild(el('p', 'muted', `提问 ${q} · 提示 ${h} · 验证 ${v}`));

      if (g.qa_history && g.qa_history.length) {
        const details = el('details', 'qa');
        details.appendChild(el('summary', null, `问答记录（${g.qa_history.length}）`));
        const ol = el('ol');
        g.qa_history.forEach((qa) => {
          const li = el('li');
          li.appendChild(el('span', 'qa-q', qa.question));
          li.appendChild(el('span', 'qa-a', qa.answer));
          ol.appendChild(li);
        });
        details.appendChild(ol);
        card.appendChild(details);
      }

      const actions = el('div', 'card-actions');
      const end = el('button', 'btn btn-sm btn-danger', '强制结束');
      end.onclick = async () => {
        if (!confirm(`强制结束「${g.label}」的对局？`)) return;
        await guard(api.post('games/end', { key: g.key }), '已结束');
        loadGames();
      };
      actions.appendChild(end);
      card.appendChild(actions);
      list.appendChild(card);
    });
  }

  // ─────────────────────────────────────────── 弹窗

  function openEditor(item) {
    state.editing = item || null;
    $('#editorTitle').textContent = item ? '编辑题目' : '新增题目';
    $('#editPuzzle').value = item ? item.puzzle : '';
    $('#editAnswer').value = '';
    $('#editor').hidden = false;

    if (item) {
      // 编辑时需要原汤底做初值，这里是明确的编辑意图，直接拉取
      api.get('story/answer', { source: state.source, id: item.id })
        .then((data) => { $('#editAnswer').value = data.answer; })
        .catch((err) => toast(err.message, 'error'));
    }
  }

  async function saveEditor() {
    const puzzle = $('#editPuzzle').value.trim();
    const answer = $('#editAnswer').value.trim();
    if (!puzzle || !answer) return toast('汤面和汤底都不能为空', 'error');

    const target = currentSource().readonly ? 'custom' : state.source;
    if (state.editing) {
      await guard(api.post('story/update', {
        source: state.source, id: state.editing.id, puzzle, answer,
      }), '已保存');
    } else {
      await guard(api.post('story/create', { source: target, puzzle, answer }), '已新增');
      if (target !== state.source) {
        state.source = target;
        renderSourceSeg();
      }
    }
    $('#editor').hidden = true;
    await loadOverview();
    await loadStories();
  }

  async function runGenerate() {
    const count = Math.min(Math.max(parseInt($('#genCount').value, 10) || 1, 1), 5);
    const box = $('#genResult');
    const btn = $('#genRun');
    btn.disabled = true;
    btn.textContent = '生成中…';
    box.hidden = false;
    box.innerHTML = '';
    box.appendChild(el('p', 'muted', `正在生成 ${count} 道题，LLM 出题较慢，请稍候…`));
    try {
      const data = await api.post('story/generate', { count });
      box.innerHTML = '';
      (data.created || []).forEach((item) => {
        const row = el('div', 'gen-item');
        row.appendChild(el('span', 'tag tag-used', '已入库'));
        row.appendChild(el('span', null, item.puzzle));
        box.appendChild(row);
      });
      (data.failed || []).forEach((reason) => {
        const row = el('div', 'gen-item');
        row.appendChild(el('span', 'tag tag-muted', '失败'));
        row.appendChild(el('span', 'muted', reason));
        box.appendChild(row);
      });
      toast(`生成完成：成功 ${(data.created || []).length} 道`, 'ok');
      state.source = 'local';
      renderSourceSeg();
      await loadOverview();
      await loadStories();
    } catch (err) {
      box.innerHTML = '';
      box.appendChild(el('p', 'error-text', err.message));
    } finally {
      btn.disabled = false;
      btn.textContent = '开始生成';
    }
  }

  // ─────────────────────────────────────────── 交互绑定

  function switchTab(name) {
    document.querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('is-active', t.dataset.tab === name);
    });
    document.querySelectorAll('.panel').forEach((p) => {
      p.hidden = p.dataset.panel !== name;
    });
    if (name === 'sessions') loadSessions();
    if (name === 'games') loadGames();
  }

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.onclick = () => switchTab(tab.dataset.tab);
  });

  let searchTimer;
  $('#search').oninput = (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.keyword = e.target.value.trim();
      state.page = 1;
      loadStories();
    }, 260);
  };
  $('#searchAnswer').onchange = (e) => {
    state.searchAnswer = e.target.checked;
    if (state.keyword) loadStories();
  };
  $('#sessionFilter').onchange = (e) => {
    state.session = e.target.value;
    state.page = 1;
    loadOverview().then(loadStories);
  };
  $('#prev').onclick = () => { if (state.page > 1) { state.page--; loadStories(); } };
  $('#next').onclick = () => { state.page++; loadStories(); };
  $('#refresh').onclick = () => { loadOverview().then(loadStories); };
  $('#btnCreate').onclick = () => openEditor(null);
  $('#editSave').onclick = saveEditor;
  $('#btnGenerate').onclick = () => { $('#genResult').hidden = true; $('#genModal').hidden = false; };
  $('#genRun').onclick = runGenerate;
  $('#resetAll').onclick = async () => {
    if (!confirm('重置所有会话的出题记录？\n所有群和私聊的记录都会清空。')) return;
    await guard(api.post('usage/reset', {}), '已全部重置');
    loadSessions();
  };
  document.querySelectorAll('[data-close]').forEach((btn) => {
    btn.onclick = () => { btn.closest('.modal').hidden = true; };
  });
  document.querySelectorAll('.modal').forEach((modal) => {
    modal.onclick = (e) => { if (e.target === modal) modal.hidden = true; };
  });

  // 启动
  loadOverview().then(loadStories).catch((err) => {
    $('#list').appendChild(el('div', 'empty', `加载失败：${err.message}`));
  });
})();
