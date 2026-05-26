const API_PREFIX = "/api/v1";
const DEFAULT_MODEL_ID = "default";
const NONE_MODEL_ID = "none";
const ADD_MODEL_OPTION = "__add_model__";
const DEFAULT_EMBEDDING_ID = "default";
const MOCK_EMBEDDING_ID = "mock";
const ADD_EMBEDDING_OPTION = "__add_embedding_model__";
const DOCUMENT_STATUS_POLL_INTERVAL_MS = 2000;
const DOCUMENT_STATUS_POLL_TIMEOUT_MS = 120000;
const DEFAULT_CONVERSATION_TITLE = "新会话";
const CHAT_MODE_CHAT = "chat";
const CHAT_MODE_DOCS = "docs";
const WORKSPACE_VIEW_CHAT = "chat";
const WORKSPACE_VIEW_AGENT_APPS = "agent_apps";
const WORKSPACE_VIEW_FAVORITES = "favorites";
const RAIL_MODE_COLLAPSED = "collapsed";
const RAIL_MODE_EXPANDED = "expanded";
const WORKSPACE_STAGE_LAUNCH = "launch";
const WORKSPACE_STAGE_CHAT = "chat";
const ACTIVE_SURFACE_NONE = "none";
const ACTIVE_SURFACE_CONVERSATIONS = "conversations";
const ACTIVE_SURFACE_FILES = "files";
const ACTIVE_SURFACE_SETTINGS = "settings";
const AUTH_MODE_LOGIN = "login";
const AUTH_MODE_REGISTER = "register";
const VALIDATION_FIELD_LABELS = {
  user_id: "账号 ID",
  display_name: "显示名称",
  password: "密码",
  confirm_password: "确认密码",
  current_password: "当前密码",
  new_password: "新密码",
  confirm_new_password: "确认新密码",
};

function createEmptyMessageCaptureState() {
  return {
    favoritesByMessageId: {},
  };
}

function createEmptyFavoriteWorkspaceAssetState() {
  return {
    memoriesByMessageId: {},
    conclusionsByMessageId: {},
    libraryDocsByMessageId: {},
  };
}

const STORAGE_KEYS = {
  conversationId: "caibao.conversationId",
  railMode: "caibao.railMode",
  legacyTeamId: "caibao.teamId",
  legacyTeamName: "caibao.teamName",
  legacyUserId: "caibao.userId",
  legacyDisplayName: "caibao.displayName",
  pendingFreshConversationPrefix: "caibao.pendingFreshConversation",
  selectedModelPrefix: "caibao.selectedModel",
  selectedEmbeddingPrefix: "caibao.selectedEmbedding",
};

const state = {
  teamId: "",
  teamName: "",
  userId: "",
  displayName: "",
  conversationId: "",
  selectedModel: DEFAULT_MODEL_ID,
  selectedEmbedding: DEFAULT_EMBEDDING_ID,
  modelConfigs: [],
  embeddingConfigs: [],
  conversations: [],
  history: [],
  documents: [],
  agentApps: [],
  agentTools: [],
  selectedAgentAppId: "",
  favoriteItems: [],
  favoriteWorkspaceAssets: createEmptyFavoriteWorkspaceAssetState(),
  selectedFavoriteId: "",
  selectedDocumentIds: [],
  agentMode: false,
  chatMode: CHAT_MODE_CHAT,
  workspaceView: WORKSPACE_VIEW_CHAT,
  railMode: RAIL_MODE_COLLAPSED,
  workspaceStage: WORKSPACE_STAGE_LAUNCH,
  activeSurface: ACTIVE_SURFACE_NONE,
  requiresFreshConversation: false,
  authMode: AUTH_MODE_LOGIN,
  sending: false,
  importing: false,
  agentAppSaving: false,
  agentAppTesting: false,
  dragCounter: 0,
  messageCaptures: createEmptyMessageCaptureState(),
  pendingCaptureActions: {},
  messageCaptureRequestSeq: 0,
  favoriteWorkspaceRequestSeq: 0,
};

const els = {};
let toastTimer = null;
let pendingAssistantRow = null;
let refreshSessionPromise = null;

document.addEventListener("DOMContentLoaded", () => {
  bindElements();
  hydrateState();
  bindEvents();
  updateIdentityCard();
  initModelOptions();
  initEmbeddingOptions();
  refreshComposerChrome();
  syncSendButtonState();
  refreshWorkspaceUi();
  bootstrapAuthSession().catch((error) => showToast(error.message, true));
});

function bindElements() {
  els.shell = document.querySelector(".shell");
  els.workspaceRail = document.getElementById("workspaceRail");
  els.railToggleBtn = document.getElementById("railToggleBtn");
  els.conversation = document.querySelector(".conversation");
  els.historyList = document.getElementById("historyList");
  els.documentList = document.getElementById("documentList");
  els.historySectionTitle = document.getElementById("historySectionTitle");
  els.documentSectionTitle = document.getElementById("documentSectionTitle");
  els.conversationCountValue = document.getElementById("conversationCountValue");
  els.documentCountValue = document.getElementById("documentCountValue");
  els.readyDocumentCountValue = document.getElementById("readyDocumentCountValue");
  els.profileSettingsBtn = document.getElementById("profileSettingsBtn");
  els.profileName = document.getElementById("profileName");
  els.profileTeam = document.getElementById("profileTeam");
  els.avatarText = document.getElementById("avatarText");
  els.workspaceEyebrow = document.getElementById("workspaceEyebrow");
  els.workspaceDescription = document.getElementById("workspaceDescription");
  els.chatWorkspaceBtn = document.getElementById("chatWorkspaceBtn");
  els.agentModeBtn = document.getElementById("agentModeBtn");
  els.agentAppsWorkspaceBtn = document.getElementById("agentAppsWorkspaceBtn");
  els.favoritesWorkspaceBtn = document.getElementById("favoritesWorkspaceBtn");
  els.railNewChatBtn = document.getElementById("railNewChatBtn");
  els.railConversationsBtn = document.getElementById("railConversationsBtn");
  els.railFilesBtn = document.getElementById("railFilesBtn");
  els.fileDrawer = document.getElementById("fileDrawer");
  els.conversationDrawerStatus = document.getElementById("conversationDrawerStatus");
  els.fileDrawerStatus = document.getElementById("fileDrawerStatus");
  els.chatWorkspacePanel = document.getElementById("chatWorkspacePanel");
  els.agentAppsPanel = document.getElementById("agentAppsPanel");
  els.agentAppList = document.getElementById("agentAppList");
  els.agentAppForm = document.getElementById("agentAppForm");
  els.agentAppNameInput = document.getElementById("agentAppNameInput");
  els.agentAppModeSelect = document.getElementById("agentAppModeSelect");
  els.agentAppPromptInput = document.getElementById("agentAppPromptInput");
  els.agentAppWorkflowInput = document.getElementById("agentAppWorkflowInput");
  els.agentAppToolsList = document.getElementById("agentAppToolsList");
  els.createAgentAppBtn = document.getElementById("createAgentAppBtn");
  els.saveAgentAppBtn = document.getElementById("saveAgentAppBtn");
  els.publishAgentAppBtn = document.getElementById("publishAgentAppBtn");
  els.agentAppTaskInput = document.getElementById("agentAppTaskInput");
  els.invokeAgentAppBtn = document.getElementById("invokeAgentAppBtn");
  els.agentAppDetailMeta = document.getElementById("agentAppDetailMeta");
  els.favoritesPanel = document.getElementById("favoritesPanel");
  els.favoritesListPane = document.getElementById("favoritesListPane");
  els.favoriteList = document.getElementById("favoriteList");
  els.favoriteDetailPane = document.getElementById("favoriteDetailPane");
  els.favoriteEmptyState = document.getElementById("favoriteEmptyState");
  els.favoriteDetailContent = document.getElementById("favoriteDetailContent");
  els.heroPanel = document.getElementById("heroPanel");
  els.workspacePatternField = document.getElementById("workspacePatternField");
  els.settingsModal = document.getElementById("settingsModal");
  els.closeSettingsBtn = document.getElementById("closeSettingsBtn");
  els.switchAccountBtn = document.getElementById("switchAccountBtn");
  els.logoutBtn = document.getElementById("logoutBtn");
  els.settingsStatus = document.getElementById("settingsStatus");
  els.settingsWorkspaceSummary = document.getElementById("settingsWorkspaceSummary");
  els.addModelBtn = document.getElementById("addModelBtn");
  els.addEmbeddingBtn = document.getElementById("addEmbeddingBtn");
  els.modelSelect = document.getElementById("settingsModelSelect");
  els.quickModelSelect = document.getElementById("quickModelSelect");
  els.quickModelManageBtn = document.getElementById("quickModelManageBtn");
  els.quickModelSelectInline = document.getElementById("quickModelSelectInline");
  els.quickModelManageBtnInline = document.getElementById("quickModelManageBtnInline");
  els.embeddingSelect = document.getElementById("settingsEmbeddingSelect");
  els.refreshAllBtn = document.getElementById("refreshAllBtn");
  els.heroTitle = document.getElementById("heroTitle");
  els.heroSubtitle = document.getElementById("heroSubtitle");
  els.heroAccount = document.getElementById("heroAccount");
  els.heroAccountHint = document.getElementById("heroAccountHint");
  els.heroSession = document.getElementById("heroSession");
  els.heroSessionHint = document.getElementById("heroSessionHint");
  els.heroScope = document.getElementById("heroScope");
  els.heroScopeHint = document.getElementById("heroScopeHint");
  els.scenarioCards = document.getElementById("scenarioCards");
  els.messageList = document.getElementById("messageList");
  els.composerZone = document.querySelector(".composer-zone");
  els.composerPresence = document.getElementById("composerPresence");
  els.composerScope = document.getElementById("composerScope");
  els.composerSession = document.getElementById("composerSession");
  els.composerHint = document.getElementById("composerHint");
  els.composerContextRow = document.getElementById("composerContextRow");
  els.toggleImportBtn = document.getElementById("toggleImportBtn");
  els.attachMenu = document.getElementById("attachMenu");
  els.uploadFileBtn = document.getElementById("uploadFileBtn");
  els.pasteTextBtn = document.getElementById("pasteTextBtn");
  els.importComposer = document.getElementById("importComposer");
  els.attachmentStrip = document.getElementById("attachmentStrip");
  els.messageInput = document.getElementById("messageInput");
  els.sendBtn = document.getElementById("sendBtn");
  els.fileInput = document.getElementById("fileInput");
  els.sourceName = document.getElementById("sourceName");
  els.fileContent = document.getElementById("fileContent");
  els.importBtn = document.getElementById("importBtn");
  els.cancelImportBtn = document.getElementById("cancelImportBtn");
  els.previewDrawer = document.getElementById("previewDrawer");
  els.previewBackdrop = document.getElementById("previewBackdrop");
  els.closePreviewBtn = document.getElementById("closePreviewBtn");
  els.previewEyebrow = document.getElementById("previewEyebrow");
  els.previewTitle = document.getElementById("previewTitle");
  els.downloadPreviewBtn = document.getElementById("downloadPreviewBtn");
  els.previewMeta = document.getElementById("previewMeta");
  els.previewSnippet = document.getElementById("previewSnippet");
  els.previewMedia = document.getElementById("previewMedia");
  els.previewContent = document.getElementById("previewContent");
  els.imageViewer = document.getElementById("imageViewer");
  els.imageViewerBackdrop = document.getElementById("imageViewerBackdrop");
  els.imageViewerTitle = document.getElementById("imageViewerTitle");
  els.imageViewerMeta = document.getElementById("imageViewerMeta");
  els.imageViewerImg = document.getElementById("imageViewerImg");
  els.imageViewerCaption = document.getElementById("imageViewerCaption");
  els.downloadImageBtn = document.getElementById("downloadImageBtn");
  els.openOriginalImageBtn = document.getElementById("openOriginalImageBtn");
  els.closeImageViewerBtn = document.getElementById("closeImageViewerBtn");
  els.authModal = document.getElementById("authModal");
  els.authLoginTab = document.getElementById("authLoginTab");
  els.authRegisterTab = document.getElementById("authRegisterTab");
  els.authError = document.getElementById("authError");
  els.loginAuthForm = document.getElementById("loginAuthForm");
  els.registerAuthForm = document.getElementById("registerAuthForm");
  els.loginUserIdInput = document.getElementById("loginUserIdInput");
  els.loginPasswordInput = document.getElementById("loginPasswordInput");
  els.registerUserIdInput = document.getElementById("registerUserIdInput");
  els.registerDisplayNameInput = document.getElementById("registerDisplayNameInput");
  els.registerPasswordInput = document.getElementById("registerPasswordInput");
  els.registerConfirmPasswordInput = document.getElementById("registerConfirmPasswordInput");
  els.cancelAuthBtn = document.getElementById("cancelAuthBtn");
  els.loginAuthBtn = document.getElementById("loginAuthBtn");
  els.registerAuthBtn = document.getElementById("registerAuthBtn");
  els.customModelModal = document.getElementById("customModelModal");
  els.customModelNameInput = document.getElementById("customModelNameInput");
  els.customModelBaseUrlInput = document.getElementById("customModelBaseUrlInput");
  els.customModelApiKeyInput = document.getElementById("customModelApiKeyInput");
  els.closeCustomModelBtn = document.getElementById("closeCustomModelBtn");
  els.cancelCustomModelBtn = document.getElementById("cancelCustomModelBtn");
  els.saveCustomModelBtn = document.getElementById("saveCustomModelBtn");
  els.customEmbeddingModal = document.getElementById("customEmbeddingModal");
  els.customEmbeddingNameInput = document.getElementById("customEmbeddingNameInput");
  els.customEmbeddingProviderInput = document.getElementById("customEmbeddingProviderSelect");
  els.customEmbeddingBaseUrlField = document.getElementById("customEmbeddingBaseUrlField");
  els.customEmbeddingBaseUrlInput = document.getElementById("customEmbeddingBaseUrlInput");
  els.customEmbeddingApiKeyField = document.getElementById("customEmbeddingApiKeyField");
  els.customEmbeddingApiKeyInput = document.getElementById("customEmbeddingApiKeyInput");
  els.closeCustomEmbeddingBtn = document.getElementById("closeCustomEmbeddingBtn");
  els.cancelCustomEmbeddingBtn = document.getElementById("cancelCustomEmbeddingBtn");
  els.saveCustomEmbeddingBtn = document.getElementById("saveCustomEmbeddingBtn");
  els.toast = document.getElementById("toast");
}

function bindEvents() {
  if (els.profileSettingsBtn) {
    els.profileSettingsBtn.addEventListener("click", openSettingsModal);
  }
  if (els.railToggleBtn) {
    els.railToggleBtn.addEventListener("click", toggleRailMode);
  }
  if (els.closeSettingsBtn) {
    els.closeSettingsBtn.addEventListener("click", closeSettingsModal);
  }
  if (els.switchAccountBtn) {
    els.switchAccountBtn.addEventListener("click", () => {
      closeSettingsModal();
      openAuthModal(AUTH_MODE_LOGIN);
    });
  }
  if (els.logoutBtn) {
    els.logoutBtn.addEventListener("click", () => {
      handleLogout().catch((error) => showToast(error.message, true));
    });
  }
  if (els.addModelBtn) {
    els.addModelBtn.addEventListener("click", openCustomModelModal);
  }
  if (els.addEmbeddingBtn) {
    els.addEmbeddingBtn.addEventListener("click", openCustomEmbeddingModal);
  }
  if (els.authLoginTab) {
    els.authLoginTab.addEventListener("click", () => {
      setAuthMode(AUTH_MODE_LOGIN);
    });
  }
  if (els.authRegisterTab) {
    els.authRegisterTab.addEventListener("click", () => {
      setAuthMode(AUTH_MODE_REGISTER);
    });
  }
  els.cancelAuthBtn.addEventListener("click", () => {
    if (!state.teamId || !state.userId) {
      showToast("请先登录后再开始聊天", true);
      return;
    }
    closeAuthModal();
  });
  if (els.loginAuthForm) {
    els.loginAuthForm.addEventListener("submit", (event) => {
      event.preventDefault();
      handleLoginSubmit().catch((error) => showToast(error.message, true));
    });
  }
  if (els.registerAuthForm) {
    els.registerAuthForm.addEventListener("submit", (event) => {
      event.preventDefault();
      handleRegisterSubmit().catch((error) => showToast(error.message, true));
    });
  }
  if (els.closeCustomModelBtn) {
    els.closeCustomModelBtn.addEventListener("click", closeCustomModelModal);
  }
  els.cancelCustomModelBtn.addEventListener("click", closeCustomModelModal);
  els.saveCustomModelBtn.addEventListener("click", () => {
    saveCustomModelConfig().catch((error) => showToast(error.message, true));
  });
  if (els.closeCustomEmbeddingBtn) {
    els.closeCustomEmbeddingBtn.addEventListener("click", closeCustomEmbeddingModal);
  }
  els.cancelCustomEmbeddingBtn.addEventListener("click", closeCustomEmbeddingModal);
  els.saveCustomEmbeddingBtn.addEventListener("click", () => {
    saveCustomEmbeddingConfig().catch((error) => showToast(error.message, true));
  });
  if (els.customEmbeddingProviderInput) {
    els.customEmbeddingProviderInput.addEventListener("change", syncCustomEmbeddingFields);
  }

  els.refreshAllBtn.addEventListener("click", () => {
    loadAllData().catch((error) => showToast(error.message, true));
  });
  if (els.chatWorkspaceBtn) {
    els.chatWorkspaceBtn.addEventListener("click", () => {
      setWorkspaceView(WORKSPACE_VIEW_CHAT).catch((error) => showToast(error.message, true));
    });
  }
  if (els.agentModeBtn) {
    els.agentModeBtn.addEventListener("click", () => {
      state.agentMode = !state.agentMode;
      refreshComposerChrome();
      refreshWorkspaceUi();
    });
  }
  if (els.agentAppsWorkspaceBtn) {
    els.agentAppsWorkspaceBtn.addEventListener("click", () => {
      setWorkspaceView(WORKSPACE_VIEW_AGENT_APPS).catch((error) => showToast(error.message, true));
    });
  }
  if (els.favoritesWorkspaceBtn) {
    els.favoritesWorkspaceBtn.addEventListener("click", () => {
      setWorkspaceView(WORKSPACE_VIEW_FAVORITES).catch((error) => showToast(error.message, true));
    });
  }
  if (els.railNewChatBtn) {
    els.railNewChatBtn.onclick = null;
    els.railNewChatBtn.addEventListener("click", () => {
      createAndSwitchConversation().catch((error) => showToast(error.message, true));
    });
  }
  if (els.railConversationsBtn) {
    els.railConversationsBtn.addEventListener("click", () => {
      setActiveSurface(ACTIVE_SURFACE_NONE);
      if (state.railMode === RAIL_MODE_COLLAPSED) {
        setRailMode(RAIL_MODE_EXPANDED);
      }
      els.historyList?.querySelector(".history-item")?.focus();
      refreshWorkspaceUi();
    });
  }
  if (els.railFilesBtn) {
    els.railFilesBtn.addEventListener("click", () => {
      window.requestAnimationFrame(() => {
        const nextSurface = state.activeSurface === ACTIVE_SURFACE_FILES
          ? ACTIVE_SURFACE_NONE
          : ACTIVE_SURFACE_FILES;
        setActiveSurface(nextSurface);
        refreshWorkspaceUi();
      });
    });
  }

  if (els.modelSelect) {
    els.modelSelect.addEventListener("change", () => {
      handleModelChange(els.modelSelect).catch((error) => showToast(error.message, true));
    });
  }
  if (els.quickModelSelect) {
    els.quickModelSelect.addEventListener("change", () => {
      handleModelChange(els.quickModelSelect).catch((error) => showToast(error.message, true));
    });
  }
  if (els.quickModelSelectInline) {
    els.quickModelSelectInline.addEventListener("change", () => {
      handleModelChange(els.quickModelSelectInline).catch((error) => showToast(error.message, true));
    });
  }
  if (els.quickModelManageBtn) {
    els.quickModelManageBtn.addEventListener("click", openSettingsModal);
  }
  if (els.quickModelManageBtnInline) {
    els.quickModelManageBtnInline.addEventListener("click", openSettingsModal);
  }
  if (els.embeddingSelect) {
    els.embeddingSelect.addEventListener("change", () => {
      handleEmbeddingChange().catch((error) => showToast(error.message, true));
    });
  }

  els.toggleImportBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    els.attachMenu.classList.toggle("hidden");
  });
  els.uploadFileBtn.addEventListener("click", () => {
    closeAttachMenu();
    els.fileInput.click();
  });
  els.pasteTextBtn.addEventListener("click", () => {
    closeAttachMenu();
    showImportComposer();
  });
  els.cancelImportBtn.addEventListener("click", hideImportComposer);
  els.fileInput.addEventListener("change", handleFileInputChange);
  els.importBtn.addEventListener("click", handleImportFromComposer);
  if (els.createAgentAppBtn) {
    els.createAgentAppBtn.addEventListener("click", createDraftAgentApp);
  }
  if (els.agentAppForm) {
    els.agentAppForm.addEventListener("submit", (event) => {
      event.preventDefault();
      saveSelectedAgentApp().catch((error) => showToast(error.message, true));
    });
  }
  if (els.publishAgentAppBtn) {
    els.publishAgentAppBtn.addEventListener("click", () => {
      publishSelectedAgentApp().catch((error) => showToast(error.message, true));
    });
  }
  if (els.invokeAgentAppBtn) {
    els.invokeAgentAppBtn.addEventListener("click", () => {
      invokeSelectedAgentApp().catch((error) => showToast(error.message, true));
    });
  }

  els.sendBtn.addEventListener("click", handleSend);
  els.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSend();
    }
  });
  els.messageInput.addEventListener("input", autoGrowTextarea);

  els.previewBackdrop.addEventListener("click", closePreviewDrawer);
  els.closePreviewBtn.addEventListener("click", closePreviewDrawer);
  if (els.imageViewerBackdrop) {
    els.imageViewerBackdrop.addEventListener("click", closeImageViewer);
  }
  if (els.closeImageViewerBtn) {
    els.closeImageViewerBtn.addEventListener("click", closeImageViewer);
  }

  if (els.composerZone) {
    els.composerZone.addEventListener("dragenter", handleComposerDragEnter);
    els.composerZone.addEventListener("dragover", handleComposerDragOver);
    els.composerZone.addEventListener("dragleave", handleComposerDragLeave);
    els.composerZone.addEventListener("drop", handleComposerDrop);
  }
  window.addEventListener("dragover", preventWindowFileDrop);
  window.addEventListener("drop", preventWindowFileDrop);

  document.addEventListener("click", handleGlobalDocumentClick);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeAttachMenu();
      closePreviewDrawer();
      closeImageViewer();
      setActiveSurface(ACTIVE_SURFACE_NONE);
      refreshWorkspaceUi();
      closeAuthModal();
      closeCustomModelModal();
      closeCustomEmbeddingModal();
    }
  });

  const sceneButtons = document.querySelectorAll(".scenario-card");
  for (const button of sceneButtons) {
    button.addEventListener("click", () => {
      applyScenarioCard(button.dataset.scene || "");
    });
  }
}

function handleGlobalDocumentClick(event) {
  const target = event.target;
  if (!(target instanceof Node)) {
    return;
  }
  if (!els.attachMenu.classList.contains("hidden")) {
    const clickInside = els.attachMenu.contains(target) || els.toggleImportBtn.contains(target);
    if (!clickInside) {
      closeAttachMenu();
    }
  }
  if (state.activeSurface === ACTIVE_SURFACE_FILES) {
    const clickInsideFileDrawer = els.fileDrawer?.contains(target);
    const fileSurfaceTrigger = target instanceof Element
      ? target.closest("#composerFilesBtn, #railFilesBtn")
      : null;
    if (!clickInsideFileDrawer && !fileSurfaceTrigger) {
      setActiveSurface(ACTIVE_SURFACE_NONE);
      refreshWorkspaceUi();
    }
  }
}

function hydrateState() {
  clearLegacyIdentityStorage();
  state.teamId = "";
  state.teamName = "";
  state.userId = "";
  state.displayName = "";
  state.conversationId = localStorage.getItem(STORAGE_KEYS.conversationId) || "";
  state.railMode = loadRailModeFromStorage();
  state.selectedModel = loadSelectedModelFromStorage();
  state.selectedEmbedding = loadSelectedEmbeddingFromStorage();
  state.authMode = AUTH_MODE_LOGIN;
  syncRailMode();
}

function clearLegacyIdentityStorage() {
  localStorage.removeItem(STORAGE_KEYS.legacyTeamId);
  localStorage.removeItem(STORAGE_KEYS.legacyTeamName);
  localStorage.removeItem(STORAGE_KEYS.legacyUserId);
  localStorage.removeItem(STORAGE_KEYS.legacyDisplayName);
}

function persistConversation() {
  localStorage.setItem(STORAGE_KEYS.conversationId, state.conversationId || "");
}

function loadRailModeFromStorage() {
  const value = localStorage.getItem(STORAGE_KEYS.railMode);
  return value === RAIL_MODE_EXPANDED ? RAIL_MODE_EXPANDED : RAIL_MODE_COLLAPSED;
}

function persistRailMode() {
  localStorage.setItem(STORAGE_KEYS.railMode, state.railMode);
}

function setRailMode(mode) {
  const nextMode = mode === RAIL_MODE_EXPANDED ? RAIL_MODE_EXPANDED : RAIL_MODE_COLLAPSED;
  if (state.railMode === nextMode) {
    return;
  }
  state.railMode = nextMode;
  persistRailMode();
  syncRailMode();
}

function toggleRailMode() {
  setRailMode(state.railMode === RAIL_MODE_EXPANDED ? RAIL_MODE_COLLAPSED : RAIL_MODE_EXPANDED);
}

function syncRailMode() {
  const expanded = state.railMode === RAIL_MODE_EXPANDED;
  els.shell?.setAttribute("data-rail-mode", state.railMode);
  els.workspaceRail?.setAttribute("aria-expanded", String(expanded));
  els.workspaceRail?.setAttribute("aria-hidden", String(!expanded));
  if (els.workspaceRail && "inert" in els.workspaceRail) {
    els.workspaceRail.inert = !expanded;
  }
  if (els.railToggleBtn) {
    const railToggleLabel = expanded ? "Collapse sidebar" : "Expand sidebar";
    els.railToggleBtn.setAttribute("aria-label", railToggleLabel);
    els.railToggleBtn.setAttribute("aria-pressed", String(expanded));
    els.railToggleBtn.setAttribute("title", railToggleLabel);
  }
}

function isCompactWorkspaceLayout() {
  return window.matchMedia("(max-width: 980px)").matches;
}

function freshConversationStorageKey() {
  if (state.teamId && state.userId) {
    return `${STORAGE_KEYS.pendingFreshConversationPrefix}:${state.teamId}:${state.userId}`;
  }
  return STORAGE_KEYS.pendingFreshConversationPrefix;
}

function loadFreshConversationRequirement() {
  return localStorage.getItem(freshConversationStorageKey()) === "1";
}

function persistFreshConversationRequirement() {
  if (state.requiresFreshConversation) {
    localStorage.setItem(freshConversationStorageKey(), "1");
    return;
  }
  localStorage.removeItem(freshConversationStorageKey());
}

function setRequiresFreshConversation(required) {
  state.requiresFreshConversation = Boolean(required);
  persistFreshConversationRequirement();
}

function normalizeAuthUserId(value) {
  return String(value || "").trim().replace(/\s+/g, "_").slice(0, 64);
}

function setAuthError(message = "") {
  if (!els.authError) {
    return;
  }
  const normalized = String(message || "").trim();
  els.authError.textContent = normalized;
  els.authError.classList.toggle("hidden", !normalized);
}

function setAuthMode(mode) {
  state.authMode = mode === AUTH_MODE_REGISTER ? AUTH_MODE_REGISTER : AUTH_MODE_LOGIN;
  if (els.authLoginTab) {
    els.authLoginTab.classList.toggle("active", state.authMode === AUTH_MODE_LOGIN);
  }
  if (els.authRegisterTab) {
    els.authRegisterTab.classList.toggle("active", state.authMode === AUTH_MODE_REGISTER);
  }
  if (els.loginAuthForm) {
    els.loginAuthForm.classList.toggle("hidden", state.authMode !== AUTH_MODE_LOGIN);
  }
  if (els.registerAuthForm) {
    els.registerAuthForm.classList.toggle("hidden", state.authMode !== AUTH_MODE_REGISTER);
  }
  setAuthError("");
}

function applyAuthSession(session, { resetConversation = false } = {}) {
  state.teamId = session?.team_id || "";
  state.teamName = session?.team_name || "";
  state.userId = session?.user_id || "";
  state.displayName = session?.display_name || session?.team_name || session?.user_id || "";
  state.selectedDocumentIds = [];
  state.chatMode = CHAT_MODE_CHAT;
  state.requiresFreshConversation = loadFreshConversationRequirement();
  if (resetConversation) {
    state.conversationId = "";
    persistConversation();
  }
  clearLegacyIdentityStorage();
  state.selectedModel = loadSelectedModelFromStorage();
  state.selectedEmbedding = loadSelectedEmbeddingFromStorage();
}

function resetAuthenticatedWorkspace() {
  state.conversations = [];
  state.history = [];
  state.documents = [];
  state.agentApps = [];
  state.agentTools = [];
  state.selectedAgentAppId = "";
  state.favoriteItems = [];
  state.favoriteWorkspaceAssets = createEmptyFavoriteWorkspaceAssetState();
  state.selectedDocumentIds = [];
  state.modelConfigs = [];
  state.embeddingConfigs = [];
  state.chatMode = CHAT_MODE_CHAT;
  state.workspaceView = WORKSPACE_VIEW_CHAT;
  setRequiresFreshConversation(false);
  setWorkspaceStage(WORKSPACE_STAGE_LAUNCH);
  setActiveSurface(ACTIVE_SURFACE_NONE);
  state.sending = false;
  state.importing = false;
  state.agentAppSaving = false;
  state.agentAppTesting = false;
  resetMessageCaptureState();
  clearConversation();
  renderConversationList();
  renderDocuments();
  renderFavoriteWorkspace();
  initModelOptions();
  initEmbeddingOptions();
  syncSendButtonState();
}

function handleSignedOutState({ openAuthDialog = false } = {}) {
  setRequiresFreshConversation(false);
  state.teamId = "";
  state.teamName = "";
  state.userId = "";
  state.displayName = "";
  state.conversationId = "";
  persistConversation();
  state.selectedModel = loadSelectedModelFromStorage();
  state.selectedEmbedding = loadSelectedEmbeddingFromStorage();
  resetAuthenticatedWorkspace();
  updateIdentityCard();
  if (openAuthDialog) {
    openAuthModal(AUTH_MODE_LOGIN);
    return;
  }
  closeAuthModal();
}

async function finalizeAuthSuccess(session, toastMessage) {
  applyAuthSession(session, { resetConversation: true });
  setWorkspaceStage(WORKSPACE_STAGE_LAUNCH);
  setActiveSurface(ACTIVE_SURFACE_NONE);
  closeAuthModal();
  updateIdentityCard();
  clearConversation();
  await Promise.all([loadModelConfigs(), loadEmbeddingConfigs(), loadConversations()]);
  state.conversationId = "";
  state.history = [];
  state.documents = [];
  state.selectedDocumentIds = [];
  setRequiresFreshConversation(true);
  persistConversation();
  resetMessageCaptureState();
  renderConversationList();
  renderDocuments();
  renderFavoriteWorkspace();
  refreshWorkspaceUi();
  els.messageInput.focus();
  if (toastMessage) {
    showToast(toastMessage);
  }
}

async function bootstrapAuthSession() {
  try {
    const session = await apiRequest("/auth/me");
    applyAuthSession(session);
    closeAuthModal();
    updateIdentityCard();
    await loadAllData();
  } catch (error) {
    if (error?.status === 401) {
      handleSignedOutState({ openAuthDialog: true });
      return;
    }
    throw error;
  }
}

function selectedModelStorageKey() {
  if (state.teamId && state.userId) {
    return `${STORAGE_KEYS.selectedModelPrefix}:${state.teamId}:${state.userId}`;
  }
  return STORAGE_KEYS.selectedModelPrefix;
}

function loadSelectedModelFromStorage() {
  return localStorage.getItem(selectedModelStorageKey()) || DEFAULT_MODEL_ID;
}

function persistSelectedModel() {
  localStorage.setItem(selectedModelStorageKey(), state.selectedModel || DEFAULT_MODEL_ID);
}

function selectedEmbeddingStorageKey() {
  if (state.teamId && state.userId) {
    return `${STORAGE_KEYS.selectedEmbeddingPrefix}:${state.teamId}:${state.userId}`;
  }
  return STORAGE_KEYS.selectedEmbeddingPrefix;
}

function loadSelectedEmbeddingFromStorage() {
  return localStorage.getItem(selectedEmbeddingStorageKey()) || DEFAULT_EMBEDDING_ID;
}

function persistSelectedEmbedding() {
  localStorage.setItem(selectedEmbeddingStorageKey(), state.selectedEmbedding || DEFAULT_EMBEDDING_ID);
}

function legacyUpdateIdentityCard() {
  updateIdentityCard();
}

function getActiveConversation() {
  return state.conversations.find((item) => item.conversation_id === state.conversationId) || null;
}

function getActiveSpaceId() {
  return getActiveConversation()?.space_id || state.history[0]?.space_id || "";
}

function resetMessageCaptureState() {
  state.messageCaptures = createEmptyMessageCaptureState();
  state.favoriteItems = [];
  state.favoriteWorkspaceAssets = createEmptyFavoriteWorkspaceAssetState();
  state.pendingCaptureActions = {};
  state.messageCaptureRequestSeq += 1;
  state.favoriteWorkspaceRequestSeq += 1;
}

function createCaptureActionKey(messageId, action) {
  return `${action}:${messageId}`;
}

function isCaptureActionPending(messageId, action) {
  return Boolean(state.pendingCaptureActions[createCaptureActionKey(messageId, action)]);
}

function setCaptureActionPending(messageId, action, pending) {
  const key = createCaptureActionKey(messageId, action);
  const next = { ...state.pendingCaptureActions };
  if (pending) {
    next[key] = true;
  } else {
    delete next[key];
  }
  state.pendingCaptureActions = next;
}

function updateMessageCaptureRecord(type, messageId, record) {
  const current = state.messageCaptures;
  const nextBucket = {
    ...current[type],
    [messageId]: record,
  };
  state.messageCaptures = {
    ...current,
    [type]: nextBucket,
  };
}

function removeMessageCaptureRecord(type, messageId) {
  const current = state.messageCaptures;
  if (!current[type]?.[messageId]) {
    return;
  }
  const nextBucket = { ...current[type] };
  delete nextBucket[messageId];
  state.messageCaptures = {
    ...current,
    [type]: nextBucket,
  };
}

function getMessageCaptureRecord(type, messageId) {
  if (!messageId) {
    return null;
  }
  return state.messageCaptures[type]?.[messageId] || null;
}

function updateFavoriteWorkspaceAsset(type, messageId, record) {
  const current = state.favoriteWorkspaceAssets;
  const nextBucket = {
    ...current[type],
    [messageId]: record,
  };
  state.favoriteWorkspaceAssets = {
    ...current,
    [type]: nextBucket,
  };
}

function removeFavoriteWorkspaceAsset(type, messageId) {
  const current = state.favoriteWorkspaceAssets;
  if (!current[type]?.[messageId]) {
    return;
  }
  const nextBucket = { ...current[type] };
  delete nextBucket[messageId];
  state.favoriteWorkspaceAssets = {
    ...current,
    [type]: nextBucket,
  };
}

function removeFavoriteWorkspaceAssets(messageId) {
  removeFavoriteWorkspaceAsset("memoriesByMessageId", messageId);
  removeFavoriteWorkspaceAsset("conclusionsByMessageId", messageId);
  removeFavoriteWorkspaceAsset("libraryDocsByMessageId", messageId);
}

function getFavoriteWorkspaceAsset(type, messageId) {
  if (!messageId) {
    return null;
  }
  return state.favoriteWorkspaceAssets[type]?.[messageId] || null;
}

function setFavoriteItems(items) {
  state.favoriteItems = Array.isArray(items) ? [...items] : [];
  if (!state.favoriteItems.some((item) => item.favorite_id === state.selectedFavoriteId)) {
    state.selectedFavoriteId = state.favoriteItems[0]?.favorite_id || "";
  }
}

function upsertFavoriteItem(item) {
  if (!item?.favorite_id) {
    return;
  }
  const next = state.favoriteItems.filter((favorite) => favorite.favorite_id !== item.favorite_id);
  state.favoriteItems = [item, ...next];
}

function removeFavoriteItem(favoriteId) {
  if (!favoriteId) {
    return;
  }
  state.favoriteItems = state.favoriteItems.filter((item) => item.favorite_id !== favoriteId);
  if (state.selectedFavoriteId === favoriteId) {
    state.selectedFavoriteId = state.favoriteItems[0]?.favorite_id || "";
  }
}

function setSelectedFavoriteId(favoriteId) {
  state.selectedFavoriteId = favoriteId || "";
  renderFavoriteWorkspace();
}

function getReadyDocumentCount() {
  return state.documents.filter((doc) => normalizeStatus(doc.status) === "ready").length;
}

function getProcessingDocumentCount() {
  return state.documents.filter((doc) => {
    const status = normalizeStatus(doc.status);
    return !["ready", "failed", "deleted"].includes(status);
  }).length;
}

function formatModelDisplayName(model) {
  return formatModelOptionLabel(model || DEFAULT_MODEL_ID).replace(" (.env)", "");
}

function formatEmbeddingDisplayName(model) {
  return formatEmbeddingOptionLabel(model || DEFAULT_EMBEDDING_ID).replace(" (.env)", "");
}

function getChatModeHint(readyCount, processingCount) {
  if (!ensureIdentity()) {
    return "设置工作台后默认直接聊天；有 ready 文件时会自动接入资料。";
  }
  if (!readyCount) {
    return processingCount
      ? "当前默认直接聊天；文件就绪后会自动参与回答。"
      : "当前默认直接聊天；需要依据时上传资料，ready 后会自动参与回答。";
  }
  return state.selectedDocumentIds.length
    ? `已自动接入 ${state.selectedDocumentIds.length} 个选中文件参与回答。`
    : `已自动接入本会话全部 ${readyCount} 个 ready 文件参与回答。`;
}

function syncAutoChatMode() {
  const readyCount = getReadyDocumentCount();
  state.chatMode = readyCount ? CHAT_MODE_DOCS : CHAT_MODE_CHAT;
  if (!readyCount) {
    state.selectedDocumentIds = [];
  }
}

function setWorkspaceStage(stage) {
  state.workspaceStage = stage === WORKSPACE_STAGE_CHAT
    ? WORKSPACE_STAGE_CHAT
    : WORKSPACE_STAGE_LAUNCH;
  syncWorkspaceStage();
}

function syncWorkspaceStage() {
  const isChatStage = state.workspaceStage === WORKSPACE_STAGE_CHAT;
  els.shell?.classList.toggle("workspace-stage-chat", isChatStage);
  els.shell?.classList.toggle("workspace-stage-launch", !isChatStage);
  els.heroPanel?.classList.toggle("hidden", isChatStage);
  els.workspacePatternField?.classList.toggle("is-muted", isChatStage);
  els.conversation?.classList.toggle("has-messages", isChatStage);
}

function getDocumentScopeSummary() {
  const readyCount = getReadyDocumentCount();
  const processingCount = getProcessingDocumentCount();
  const explicitSelected = getExplicitSelectedDocumentIds();

  if (!readyCount) {
    if (state.documents.length) {
      return {
        label: "等待文件就绪",
        hint: processingCount
          ? `${processingCount} 个文件仍在处理中，你现在也可以直接聊天，完成后会自动参与检索`
          : "当前没有可检索的 ready 文件，但仍可直接聊天，请检查处理状态",
      };
    }
    return {
      label: "0 个文件",
      hint: "当前还没添加资料，也可以直接聊天；上传后可按文件范围检索、问答和总结",
    };
  }

  if (explicitSelected.length) {
    return {
      label: `${explicitSelected.length} 个指定文件`,
      hint: "本轮只检索选中的 ready 文件，范围更精确",
    };
  }

  return {
    label: `${readyCount} 个 ready 文件`,
    hint: processingCount
      ? `已自动接入全部 ready 文件参与回答，另有 ${processingCount} 个文件正在处理中`
      : "已自动接入本会话全部 ready 文件，也可以手动缩小范围",
  };
}

async function setWorkspaceView(view) {
  const nextView = view === WORKSPACE_VIEW_AGENT_APPS
    ? WORKSPACE_VIEW_AGENT_APPS
    : (view === WORKSPACE_VIEW_FAVORITES ? WORKSPACE_VIEW_FAVORITES : WORKSPACE_VIEW_CHAT);
  state.workspaceView = nextView;
  refreshWorkspaceUi();

  if (nextView === WORKSPACE_VIEW_FAVORITES) {
    renderFavoriteWorkspace();
    await loadFavoriteWorkspaceAssets();
  } else if (nextView === WORKSPACE_VIEW_AGENT_APPS) {
    await Promise.all([loadAgentTools(), loadAgentApps()]);
  }
}

function setActiveSurface(surface) {
  if (
    surface !== ACTIVE_SURFACE_CONVERSATIONS
    && surface !== ACTIVE_SURFACE_FILES
    && surface !== ACTIVE_SURFACE_SETTINGS
  ) {
    surface = ACTIVE_SURFACE_NONE;
  }
  state.activeSurface = surface || ACTIVE_SURFACE_NONE;
  syncActiveSurface();
}

function syncActiveSurface() {
  const current = state.activeSurface || ACTIVE_SURFACE_NONE;
  els.fileDrawer?.classList.toggle("hidden", current !== ACTIVE_SURFACE_FILES);
  els.fileDrawer?.setAttribute("aria-hidden", String(current !== ACTIVE_SURFACE_FILES));
  els.settingsModal?.classList.toggle("hidden", current !== ACTIVE_SURFACE_SETTINGS);
  els.settingsModal?.setAttribute("aria-hidden", String(current !== ACTIVE_SURFACE_SETTINGS));
}

function syncWorkspaceView() {
  const isFavoritesView = state.workspaceView === WORKSPACE_VIEW_FAVORITES;
  const isAgentAppsView = state.workspaceView === WORKSPACE_VIEW_AGENT_APPS;
  const isChatView = state.workspaceView === WORKSPACE_VIEW_CHAT;

  if (els.chatWorkspaceBtn) {
    els.chatWorkspaceBtn.classList.toggle("active", isChatView);
    els.chatWorkspaceBtn.setAttribute("aria-pressed", String(isChatView));
  }
  if (els.agentAppsWorkspaceBtn) {
    els.agentAppsWorkspaceBtn.classList.toggle("active", isAgentAppsView);
    els.agentAppsWorkspaceBtn.setAttribute("aria-pressed", String(isAgentAppsView));
  }
  if (els.favoritesWorkspaceBtn) {
    els.favoritesWorkspaceBtn.classList.toggle("active", isFavoritesView);
    els.favoritesWorkspaceBtn.setAttribute("aria-pressed", String(isFavoritesView));
    els.favoritesWorkspaceBtn.textContent = state.favoriteItems.length
      ? `收藏夹 ${state.favoriteItems.length}`
      : "收藏夹";
  }
  if (els.agentModeBtn) {
    els.agentModeBtn.classList.toggle("active", state.agentMode);
    els.agentModeBtn.setAttribute("aria-pressed", String(state.agentMode));
  }
  if (els.chatWorkspacePanel) {
    els.chatWorkspacePanel.classList.toggle("hidden", !isChatView);
  }
  if (els.agentAppsPanel) {
    els.agentAppsPanel.classList.toggle("hidden", !isAgentAppsView);
  }
  if (els.favoritesPanel) {
    els.favoritesPanel.classList.toggle("hidden", !isFavoritesView);
  }
}

function legacyRefreshWorkspaceUi() {
  const loggedIn = ensureIdentity();
  const activeConversation = getActiveConversation();
  const scope = getDocumentScopeSummary();
  const activeTitle = activeConversation?.title || DEFAULT_CONVERSATION_TITLE;
  const readyCount = getReadyDocumentCount();

  if (els.historySectionTitle) {
    els.historySectionTitle.textContent = state.conversations.length
      ? `会话列表 · ${state.conversations.length}`
      : "会话列表";
  }
  if (els.documentSectionTitle) {
    els.documentSectionTitle.textContent = state.documents.length
      ? `本会话文件 · ${state.documents.length}`
      : "本会话文件";
  }
  if (els.conversationCountValue) {
    els.conversationCountValue.textContent = String(state.conversations.length);
  }
  if (els.documentCountValue) {
    els.documentCountValue.textContent = String(state.documents.length);
  }
  if (els.readyDocumentCountValue) {
    els.readyDocumentCountValue.textContent = String(readyCount);
  }

  if (els.workspaceEyebrow) {
    els.workspaceEyebrow.textContent = loggedIn
      ? `当前工作台 · ${state.displayName || state.teamId}`
      : "Personal AI Assistant";
  }
  if (els.workspaceDescription) {
    if (!loggedIn) {
      els.workspaceDescription.textContent = "进入工作台后即可直接聊天；需要时再导入文档、限定检索范围和查看引用来源。";
    } else {
      const desc = [activeTitle];
      if (state.history.length) {
        desc.push(`${state.history.length} 轮对话`);
      }
      desc.push(scope.label);
      els.workspaceDescription.textContent = desc.join(" · ");
    }
  }
  if (els.settingsWorkspaceSummary) {
    els.settingsWorkspaceSummary.textContent = loggedIn
      ? `${state.displayName || state.teamId} · 工作台 ID ${state.teamId}`
      : "未进入工作台";
  }

  if (els.heroTitle) {
    if (!loggedIn) {
      els.heroTitle.textContent = "设置工作台后直接开始聊天，需要时再补充资料";
    } else if (!state.documents.length) {
      els.heroTitle.textContent = "直接开始聊天，需要时再补充资料";
    } else if (!readyCount) {
      els.heroTitle.textContent = "现在就能继续聊，资料处理完成后会自动增强回答";
    } else {
      els.heroTitle.textContent = "直接聊，或让资料为回答补充依据";
    }
  }
  if (els.heroSubtitle) {
    if (!loggedIn) {
      els.heroSubtitle.textContent = "CaiBao 支持直接对话、文件导入、引用溯源、图片预览，以及按需启用的高级模型配置。";
    } else {
      els.heroSubtitle.textContent = scope.hint;
    }
  }
  if (els.heroAccount) {
    els.heroAccount.textContent = state.displayName || state.teamName || "未设置";
  }
  if (els.heroAccountHint) {
    els.heroAccountHint.textContent = loggedIn
      ? `工作台 ID · ${state.teamId}`
      : "点击左下角设置工作台";
  }
  if (els.heroSession) {
    els.heroSession.textContent = activeTitle;
  }
  if (els.heroSessionHint) {
    els.heroSessionHint.textContent = loggedIn
      ? (state.history.length
        ? `${state.history.length} 轮消息会沿用当前工作台上下文`
        : "进入工作台后会自动为你创建新会话")
      : "进入工作台后自动接续最近上下文";
  }
  if (els.heroScope) {
    els.heroScope.textContent = scope.label;
  }
  if (els.heroScopeHint) {
    els.heroScopeHint.textContent = scope.hint;
  }

  if (els.composerPresence) {
    els.composerPresence.textContent = state.sending
      ? "CaiBao 正在整理回答"
      : (loggedIn ? `工作台 · ${state.displayName || state.teamId}` : "尚未设置工作台");
  }
  if (els.composerScope) {
    els.composerScope.textContent = `资料范围 · ${scope.label}`;
  }
  if (els.composerSession) {
    els.composerSession.textContent = `会话 · ${activeTitle}`;
  }
  if (els.composerHint) {
    if (state.importing) {
      els.composerHint.textContent = "正在处理新资料，完成后会自动加入本会话检索范围。";
    } else if (!readyCount) {
      els.composerHint.textContent = "支持直接聊天、拖拽上传、粘贴文本和双击附件预览。";
    } else {
      els.composerHint.textContent = "回答会附带来源卡片，双击附件可快速预览原文。";
    }
  }
  if (els.chatOnlyBtn) {
    const isChatMode = state.chatMode === CHAT_MODE_CHAT;
    els.chatOnlyBtn.classList.toggle("active", isChatMode);
    els.chatOnlyBtn.setAttribute("aria-pressed", String(isChatMode));
  }
  if (els.docAssistBtn) {
    const isDocsMode = state.chatMode === CHAT_MODE_DOCS;
    const docsAvailable = readyCount > 0;
    els.docAssistBtn.classList.toggle("active", isDocsMode);
    els.docAssistBtn.setAttribute("aria-pressed", String(isDocsMode));
    els.docAssistBtn.disabled = !docsAvailable;
  }
  if (els.chatModeHint) {
    els.chatModeHint.textContent = getChatModeHint(readyCount, getProcessingDocumentCount());
  }
  if (els.heroPanel && els.conversation) {
    if (!state.history.length && !pendingAssistantRow) {
      els.heroPanel.classList.remove("hidden");
      els.conversation.classList.remove("has-messages");
    }
  }

  if (els.shell) {
    els.shell.classList.toggle("has-history", Boolean(state.history.length || pendingAssistantRow));
  }

  syncWorkspaceView();
}

function legacyInitModelOptions() {
  const configuredModels = state.modelConfigs.map((item) => item.model_name);
  const allModels = dedupeStrings([DEFAULT_MODEL_ID, NONE_MODEL_ID, ...configuredModels]);
  els.modelSelect.innerHTML = "";

  for (const model of allModels) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = formatModelOptionLabel(model);
    els.modelSelect.appendChild(option);
  }

  const addOption = document.createElement("option");
  addOption.value = ADD_MODEL_OPTION;
  addOption.textContent = "新增模型...";
  els.modelSelect.appendChild(addOption);

  state.selectedModel = allModels.includes(state.selectedModel) ? state.selectedModel : DEFAULT_MODEL_ID;
  els.modelSelect.value = state.selectedModel;
  persistSelectedModel();
  refreshWorkspaceUi();
}

function legacyInitEmbeddingOptions() {
  const configuredModels = state.embeddingConfigs.map((item) => item.model_name);
  const allModels = dedupeStrings([DEFAULT_EMBEDDING_ID, MOCK_EMBEDDING_ID, ...configuredModels]);
  els.embeddingSelect.innerHTML = "";

  for (const model of allModels) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = formatEmbeddingOptionLabel(model);
    els.embeddingSelect.appendChild(option);
  }

  const addOption = document.createElement("option");
  addOption.value = ADD_EMBEDDING_OPTION;
  addOption.textContent = "新增向量模型...";
  els.embeddingSelect.appendChild(addOption);

  state.selectedEmbedding = allModels.includes(state.selectedEmbedding) ? state.selectedEmbedding : DEFAULT_EMBEDDING_ID;
  els.embeddingSelect.value = state.selectedEmbedding;
  persistSelectedEmbedding();
  refreshWorkspaceUi();
}

function formatModelOptionLabel(model) {
  if (model === DEFAULT_MODEL_ID) {
    return "default (.env)";
  }
  if (model === NONE_MODEL_ID) {
    return "none (mock)";
  }
  return model;
}

function formatEmbeddingOptionLabel(model) {
  if (model === DEFAULT_EMBEDDING_ID) {
    return "default (.env)";
  }
  if (model === MOCK_EMBEDDING_ID) {
    return "mock (hashing)";
  }
  return model;
}
async function legacyHandleModelChange() {
  const selected = els.modelSelect.value;
  if (selected !== ADD_MODEL_OPTION) {
    state.selectedModel = selected;
    persistSelectedModel();
    if (selected === DEFAULT_MODEL_ID) {
      showToast("当前使用系统默认对话模型配置");
    } else if (selected === NONE_MODEL_ID) {
      showToast("当前使用 none（仅输出 mock 回复）");
    } else {
      showToast(`对话模型已切换为 ${selected}`);
    }
    return;
  }

  if (!ensureIdentity()) {
    openSettingsModal();
      showToast("请先登录", true);
    els.modelSelect.value = state.selectedModel;
    return;
  }

  openCustomModelModal();
}

async function legacyHandleEmbeddingChange() {
  const selected = els.embeddingSelect.value;
  if (selected !== ADD_EMBEDDING_OPTION) {
    state.selectedEmbedding = selected;
    persistSelectedEmbedding();
    if (selected === DEFAULT_EMBEDDING_ID) {
      showToast("当前使用系统默认检索向量配置");
    } else if (selected === MOCK_EMBEDDING_ID) {
      showToast("当前使用 mock（hashing 向量）");
    } else {
      showToast(`检索向量模型已切换为 ${selected}`);
    }
    return;
  }

  if (!ensureIdentity()) {
    openSettingsModal();
      showToast("请先登录", true);
    els.embeddingSelect.value = state.selectedEmbedding;
    return;
  }

  openCustomEmbeddingModal();
}

async function legacySaveCustomModelConfig() {
  const normalizedModelName = els.customModelNameInput.value.trim();
  const baseUrl = els.customModelBaseUrlInput.value.trim();
  const apiKey = els.customModelApiKeyInput.value.trim();

  if (!normalizedModelName || normalizedModelName.toLowerCase() === DEFAULT_MODEL_ID || normalizedModelName.toLowerCase() === NONE_MODEL_ID) {
    showToast("对话模型名称无效", true);
    return;
  }
  if (!baseUrl) {
    showToast("请填写对话模型 API Base URL", true);
    return;
  }
  if (!apiKey) {
    showToast("请填写对话模型 API Key", true);
    return;
  }

  setButtonLoading(els.saveCustomModelBtn, true, "保存中...");
  try {
    await apiRequest("/llm/models", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        model_name: normalizedModelName,
        base_url: baseUrl,
        api_key: apiKey,
      },
    });

    state.selectedModel = normalizedModelName;
    persistSelectedModel();
    await loadModelConfigs();
    closeCustomModelModal();
    showToast(`已添加并切换到对话模型 ${normalizedModelName}`);
  } finally {
    setButtonLoading(els.saveCustomModelBtn, false, "保存模型");
  }
}

async function legacySaveCustomEmbeddingConfig() {
  const normalizedModelName = els.customEmbeddingNameInput.value.trim();
  const provider = els.customEmbeddingProviderInput.value.trim().toLowerCase();
  const baseUrl = els.customEmbeddingBaseUrlInput.value.trim();
  const apiKey = els.customEmbeddingApiKeyInput.value.trim();

  if (!normalizedModelName || normalizedModelName.toLowerCase() === DEFAULT_EMBEDDING_ID) {
    showToast("检索向量模型名称无效", true);
    return;
  }
  if (!provider) {
    showToast("请选择向量 provider", true);
    return;
  }
  if (provider !== "mock" && !baseUrl) {
    showToast("请填写检索向量 API Base URL", true);
    return;
  }
  if (provider !== "mock" && !apiKey) {
    showToast("请填写检索向量 API Key", true);
    return;
  }

  setButtonLoading(els.saveCustomEmbeddingBtn, true, "保存中...");
  try {
    await apiRequest("/embedding/models", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        model_name: normalizedModelName,
        provider,
        base_url: provider === "mock" ? null : baseUrl,
        api_key: provider === "mock" ? null : apiKey,
      },
    });

    state.selectedEmbedding = normalizedModelName;
    persistSelectedEmbedding();
    await loadEmbeddingConfigs();
    closeCustomEmbeddingModal();
    showToast(`已添加并切换到检索向量模型 ${normalizedModelName}`);
  } finally {
    setButtonLoading(els.saveCustomEmbeddingBtn, false, "保存模型");
  }
}

async function legacyHandleSaveAuth() {
  return handleLoginSubmit();
}

async function loadAllData() {
  if (!ensureIdentity()) {
    return;
  }

  await Promise.all([loadModelConfigs(), loadEmbeddingConfigs(), loadAgentTools(), loadAgentApps()]);
  initModelOptions();
  initEmbeddingOptions();
  await loadConversations();
  if (state.requiresFreshConversation) {
    state.conversationId = "";
    state.history = [];
    state.documents = [];
    state.selectedDocumentIds = [];
    persistConversation();
    setWorkspaceStage(WORKSPACE_STAGE_LAUNCH);
    setActiveSurface(ACTIVE_SURFACE_NONE);
    clearConversation();
    resetMessageCaptureState();
    renderConversationList();
    renderDocuments();
    renderAgentApps();
    renderFavoriteWorkspace();
    refreshWorkspaceUi();
    return;
  }
  await ensureActiveConversation();
  await Promise.all([loadHistory(), loadDocuments(), loadMessageCaptures()]);
  refreshWorkspaceUi();
}

async function loadModelConfigs() {
  if (!ensureIdentity()) {
    state.modelConfigs = [];
    initModelOptions();
    return;
  }

  const query = new URLSearchParams({
    team_id: state.teamId,
    user_id: state.userId,
  });
  const response = await apiRequest(`/llm/models?${query.toString()}`);
  state.modelConfigs = Array.isArray(response.items) ? response.items : [];
  initModelOptions();
}

async function loadEmbeddingConfigs() {
  if (!ensureIdentity()) {
    state.embeddingConfigs = [];
    initEmbeddingOptions();
    return;
  }

  const query = new URLSearchParams({
    team_id: state.teamId,
    user_id: state.userId,
  });
  const response = await apiRequest(`/embedding/models?${query.toString()}`);
  state.embeddingConfigs = Array.isArray(response.items) ? response.items : [];
  initEmbeddingOptions();
}

async function loadAgentTools() {
  if (!ensureIdentity()) {
    state.agentTools = [];
    renderAgentAppTools();
    return;
  }
  const response = await apiRequest("/agent/tools");
  state.agentTools = Array.isArray(response) ? response : [];
  renderAgentAppTools();
}

async function loadAgentApps() {
  if (!ensureIdentity()) {
    state.agentApps = [];
    state.selectedAgentAppId = "";
    renderAgentApps();
    return;
  }
  const response = await apiRequest("/apps");
  state.agentApps = Array.isArray(response.items) ? response.items : [];
  if (!state.selectedAgentAppId && state.agentApps.length) {
    state.selectedAgentAppId = state.agentApps[0].app_id;
  }
  if (state.selectedAgentAppId && !state.agentApps.some((item) => item.app_id === state.selectedAgentAppId)) {
    state.selectedAgentAppId = state.agentApps[0]?.app_id || "";
  }
  renderAgentApps();
}

function getSelectedAgentApp() {
  return state.agentApps.find((item) => item.app_id === state.selectedAgentAppId) || null;
}

function selectAgentApp(appId) {
  state.selectedAgentAppId = appId || "";
  renderAgentApps();
  refreshComposerChrome();
}

function renderAgentApps() {
  renderAgentAppList();
  renderAgentAppForm();
  renderAgentAppTools();
}

function renderAgentAppList() {
  if (!els.agentAppList) {
    return;
  }
  els.agentAppList.innerHTML = "";
  if (!state.agentApps.length) {
    appendEmpty(els.agentAppList, "还没有 Agent 应用");
    return;
  }
  for (const app of state.agentApps) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `agent-app-item${app.app_id === state.selectedAgentAppId ? " active" : ""}`;
    button.addEventListener("click", () => selectAgentApp(app.app_id));

    const title = document.createElement("strong");
    title.textContent = app.name || "未命名应用";
    const meta = document.createElement("span");
    meta.textContent = `${app.mode || "agent_auto"} · v${app.app_version || 0} · ${app.status || "draft"}`;
    button.append(title, meta);
    els.agentAppList.appendChild(button);
  }
}

function renderAgentAppForm() {
  const app = getSelectedAgentApp();
  if (els.agentAppNameInput) {
    els.agentAppNameInput.value = app?.name || "";
  }
  if (els.agentAppModeSelect) {
    els.agentAppModeSelect.value = app?.mode || "agent_auto";
  }
  if (els.agentAppPromptInput) {
    els.agentAppPromptInput.value = app?.system_prompt || "";
  }
  if (els.agentAppWorkflowInput) {
    els.agentAppWorkflowInput.value = JSON.stringify(app?.workflow_config || { nodes: [] }, null, 2);
  }
  if (els.agentAppDetailMeta) {
    els.agentAppDetailMeta.textContent = app
      ? `/apps/${app.app_id}/invoke · ${app.status} · v${app.app_version || 0}`
      : "选择应用后可调用 /apps/{app_id}/invoke";
  }
  if (els.publishAgentAppBtn) {
    els.publishAgentAppBtn.disabled = !app || state.agentAppSaving;
  }
  if (els.invokeAgentAppBtn) {
    els.invokeAgentAppBtn.disabled = !app || state.agentAppTesting;
  }
}

function renderAgentAppTools() {
  if (!els.agentAppToolsList) {
    return;
  }
  const app = getSelectedAgentApp();
  const enabled = new Set(app?.tool_config?.enabled_tools || state.agentTools.map((tool) => tool.name));
  els.agentAppToolsList.innerHTML = "";
  if (!state.agentTools.length) {
    appendEmpty(els.agentAppToolsList, "工具注册表暂不可用");
    return;
  }
  for (const tool of state.agentTools) {
    const label = document.createElement("label");
    label.className = "agent-tool-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = tool.name;
    checkbox.checked = enabled.has(tool.name);
    const text = document.createElement("span");
    text.textContent = `${tool.display_name || tool.name}${tool.dangerous ? " · 需确认" : ""}`;
    label.append(checkbox, text);
    els.agentAppToolsList.appendChild(label);
  }
}

function readAgentAppToolConfig() {
  const enabledTools = [...(els.agentAppToolsList?.querySelectorAll("input[type='checkbox']:checked") || [])]
    .map((item) => item.value)
    .filter(Boolean);
  return {
    enabled_tools: enabledTools,
    dangerous_requires_confirmation: true,
  };
}

function readAgentAppWorkflowConfig() {
  const raw = (els.agentAppWorkflowInput?.value || "").trim();
  if (!raw) {
    return { nodes: [] };
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Workflow JSON 必须是对象");
    }
    return parsed;
  } catch (error) {
    throw new Error(`Workflow JSON 无效：${error.message}`);
  }
}

async function createDraftAgentApp() {
  if (!ensureIdentity()) {
    openAuthModal();
    showToast("请先登录", true);
    return;
  }
  const created = await apiRequest("/apps", {
    method: "POST",
    body: {
      name: "运维值班助手",
      description: "Dify-lite Agent App",
      mode: "agent_auto",
      system_prompt: "你是企业运维知识 Agent。回答要基于资料；创建 incident、记忆卡、结论等写入动作必须等待用户确认。",
      retrieval_config: {
        top_k: 5,
        include_memory: true,
        include_library: true,
        include_conclusions: false,
      },
      tool_config: {
        enabled_tools: state.agentTools.map((tool) => tool.name),
        dangerous_requires_confirmation: true,
      },
      workflow_config: { nodes: [] },
    },
  });
  state.agentApps = [created, ...state.agentApps.filter((item) => item.app_id !== created.app_id)];
  state.selectedAgentAppId = created.app_id;
  renderAgentApps();
  showToast("已创建 Agent 应用");
}

async function saveSelectedAgentApp() {
  const app = getSelectedAgentApp();
  if (!app) {
    showToast("请先选择 Agent 应用", true);
    return;
  }
  state.agentAppSaving = true;
  renderAgentAppForm();
  try {
    const updated = await apiRequest(`/apps/${encodeURIComponent(app.app_id)}`, {
      method: "PATCH",
      body: {
        name: els.agentAppNameInput?.value.trim() || "未命名应用",
        mode: els.agentAppModeSelect?.value || "agent_auto",
        system_prompt: els.agentAppPromptInput?.value || "",
        tool_config: readAgentAppToolConfig(),
        workflow_config: readAgentAppWorkflowConfig(),
      },
    });
    state.agentApps = state.agentApps.map((item) => item.app_id === updated.app_id ? updated : item);
    state.selectedAgentAppId = updated.app_id;
    renderAgentApps();
    showToast("Agent 应用已保存");
  } finally {
    state.agentAppSaving = false;
    renderAgentAppForm();
  }
}

async function publishSelectedAgentApp() {
  const app = getSelectedAgentApp();
  if (!app) {
    showToast("请先选择 Agent 应用", true);
    return;
  }
  await saveSelectedAgentApp();
  const version = await apiRequest(`/apps/${encodeURIComponent(app.app_id)}/publish`, {
    method: "POST",
    body: { notes: "Published from Agent App Builder" },
  });
  await loadAgentApps();
  showToast(`已发布 v${version.version_number}`);
}

async function invokeSelectedAgentApp() {
  const app = getSelectedAgentApp();
  const task = (els.agentAppTaskInput?.value || "").trim();
  if (!app || !task) {
    showToast("请选择应用并填写测试任务", true);
    return;
  }
  await ensureConversationReady();
  state.agentAppTesting = true;
  renderAgentAppForm();
  state.agentMode = true;
  setWorkspaceStage(WORKSPACE_STAGE_CHAT);
  await setWorkspaceView(WORKSPACE_VIEW_CHAT);
  appendMessage("user", task);
  appendPendingAssistantMessage("Agent 应用正在执行…");
  try {
    const body = {
      conversation_id: state.conversationId,
      space_id: getActiveSpaceId(),
      task,
      dry_run: false,
      confirm_dangerous_actions: false,
      embedding_model: state.selectedEmbedding || DEFAULT_EMBEDDING_ID,
    };
    if (state.selectedModel !== DEFAULT_MODEL_ID) {
      body.model = state.selectedModel;
    }
    const createResponse = await apiRequest(`/apps/${encodeURIComponent(app.app_id)}/runs`, {
      method: "POST",
      body,
    });
    await consumeAgentStream(createResponse.stream_url, task);
    await Promise.all([loadHistory(), loadConversations()]);
  } finally {
    removePendingAssistantMessage();
    state.agentAppTesting = false;
    renderAgentAppForm();
  }
}

function setSurfaceStatus(targetEl, message, isError = false) {
  if (!targetEl) {
    return;
  }
  targetEl.textContent = message;
  targetEl.classList.remove("hidden", "error");
  if (isError) {
    targetEl.classList.add("error");
  }
}

function clearSurfaceStatus(targetEl) {
  if (!targetEl) {
    return;
  }
  targetEl.textContent = "";
  targetEl.classList.add("hidden");
  targetEl.classList.remove("error");
}

async function loadConversations() {
  clearSurfaceStatus(els.conversationDrawerStatus);
  try {
    const query = new URLSearchParams({
      team_id: state.teamId,
      user_id: state.userId,
      limit: "100",
    });
    const response = await apiRequest(`/conversations?${query.toString()}`);
    state.conversations = Array.isArray(response) ? response : [];
    renderConversationList();
  } catch (error) {
    setSurfaceStatus(els.conversationDrawerStatus, error.message, true);
    throw error;
  }
}

async function ensureActiveConversation() {
  if (!state.conversations.length) {
    const created = await createConversation(DEFAULT_CONVERSATION_TITLE);
    state.conversations = [created];
  }

  const exists = state.conversations.some((item) => item.conversation_id === state.conversationId);
  if (!exists) {
    state.conversationId = state.conversations[0].conversation_id;
    persistConversation();
  }
  setRequiresFreshConversation(false);
  renderConversationList();
}

function renderConversationList() {
  els.historyList.innerHTML = "";
  if (!state.conversations.length) {
    appendEmpty(els.historyList, "暂无会话");
    refreshWorkspaceUi();
    return;
  }

  for (const item of state.conversations) {
    const li = document.createElement("li");
    li.className = "history-item";
    li.tabIndex = 0;
    li.classList.toggle("active", item.conversation_id === state.conversationId);
    li.classList.toggle("pinned", Boolean(item.is_pinned));

    const row = document.createElement("div");
    row.className = "history-row";

    const titleWrap = document.createElement("div");
    titleWrap.className = "history-title-wrap";

    const title = document.createElement("button");
    title.type = "button";
    title.className = "doc-card-action";
    title.textContent = item.title || "新会话";
    title.addEventListener("click", (event) => {
      event.stopPropagation();
      switchConversation(item.conversation_id).catch((error) => showToast(error.message, true));
    });
    titleWrap.appendChild(title);

    if (item.is_pinned) {
      const pinTag = document.createElement("span");
      pinTag.className = "history-pin-tag";
      pinTag.textContent = "置顶";
      titleWrap.appendChild(pinTag);
    }

    const actions = document.createElement("div");
    actions.className = "history-actions";

    const menu = document.createElement("details");
    menu.className = "history-menu";

    const summary = document.createElement("summary");
    summary.className = "history-more";
    summary.textContent = "...";

    const panel = document.createElement("div");
    panel.className = "history-menu-panel";
    panel.append(
      createHistoryActionButton("重命名", () => {
        menu.open = false;
        renameConversation(item).catch((error) => showToast(error.message, true));
      }),
      createHistoryActionButton(item.is_pinned ? "取消置顶" : "置顶", () => {
        menu.open = false;
        pinConversation(item.conversation_id, !item.is_pinned).catch((error) => showToast(error.message, true));
      }),
      createHistoryActionButton("删除", () => {
        menu.open = false;
        deleteConversation(item.conversation_id).catch((error) => showToast(error.message, true));
      }, true),
    );

    menu.append(summary, panel);
    actions.appendChild(menu);
    row.append(titleWrap, actions);
    li.appendChild(row);

    const meta = document.createElement("div");
    meta.className = "history-meta";
    const metaParts = [];
    if (item.conversation_id === state.conversationId) {
      metaParts.push("当前会话");
    }
    metaParts.push(item.status || "active", formatTime(item.created_at));
    meta.textContent = metaParts.join(" · ");
    li.appendChild(meta);

    li.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) {
        return;
      }
      if (target.closest(".history-menu")) {
        return;
      }
      switchConversation(item.conversation_id).catch((error) => showToast(error.message, true));
    });
    li.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") {
        return;
      }
      event.preventDefault();
      switchConversation(item.conversation_id).catch((error) => showToast(error.message, true));
    });

    els.historyList.appendChild(li);
  }
  refreshWorkspaceUi();
}

function createHistoryActionButton(label, onClick, danger = false) {
  const button = document.createElement("button");
  button.className = `history-action-btn${danger ? " danger" : ""}`;
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

async function createConversation(title) {
  return apiRequest("/conversations", {
    method: "POST",
    body: {
      team_id: state.teamId,
      user_id: state.userId,
      title,
    },
  });
}

async function createAndSwitchConversation({
  silent = false,
  restoreLaunch = true,
  closeSurface = true,
  focusComposer = true,
} = {}) {
  if (!ensureIdentity()) {
    openAuthModal();
      showToast("请先登录", true);
    return;
  }

  const created = await createConversation(DEFAULT_CONVERSATION_TITLE);
  setRequiresFreshConversation(false);
  state.conversationId = created.conversation_id;
  state.selectedDocumentIds = [];
  state.chatMode = CHAT_MODE_CHAT;
  persistConversation();
  await loadConversations();
  clearConversation();
  await Promise.all([loadHistory(), loadDocuments(), loadMessageCaptures()]);
  if (restoreLaunch) {
    setWorkspaceStage(WORKSPACE_STAGE_LAUNCH);
  }
  if (closeSurface) {
    setActiveSurface(ACTIVE_SURFACE_NONE);
  }
  if (restoreLaunch || closeSurface) {
    refreshWorkspaceUi();
  }
  if (focusComposer && els.messageInput) {
    els.messageInput.focus();
  }
  if (!silent) {
    showToast("已创建新会话");
  }
}

async function switchConversation(conversationId) {
  if (!conversationId || state.conversationId === conversationId) {
    return;
  }
  state.conversationId = conversationId;
  setRequiresFreshConversation(false);
  state.selectedDocumentIds = [];
  state.chatMode = CHAT_MODE_CHAT;
  persistConversation();
  renderConversationList();
  clearConversation();
  await Promise.all([loadHistory(), loadDocuments(), loadMessageCaptures()]);
}

async function renameConversation(conversation) {
  const currentTitle = String(conversation.title || "新会话").trim();
  const nextTitle = window.prompt("输入新的会话标题", currentTitle);
  if (nextTitle === null) {
    return;
  }
  const normalizedTitle = nextTitle.trim();
  if (!normalizedTitle) {
    showToast("会话标题不能为空", true);
    return;
  }

  await apiRequest(`/conversations/${encodeURIComponent(conversation.conversation_id)}`, {
    method: "PATCH",
    body: {
      team_id: state.teamId,
      user_id: state.userId,
      title: normalizedTitle,
    },
  });
  await loadConversations();
  showToast("会话标题已更新");
}

async function pinConversation(conversationId, pinned) {
  await apiRequest(`/conversations/${encodeURIComponent(conversationId)}/pin`, {
    method: "PATCH",
    body: {
      team_id: state.teamId,
      user_id: state.userId,
      pinned,
    },
  });
  await loadConversations();
  showToast(pinned ? "已置顶会话" : "已取消置顶");
}

async function deleteConversation(conversationId) {
  if (!window.confirm("确认删除这个会话吗？")) {
    return;
  }

  const query = new URLSearchParams({
    team_id: state.teamId,
    user_id: state.userId,
  });
  await apiRequest(`/conversations/${encodeURIComponent(conversationId)}?${query.toString()}`, {
    method: "DELETE",
  });

  if (state.conversationId === conversationId) {
    state.conversationId = "";
    state.selectedDocumentIds = [];
    persistConversation();
  }

  await loadConversations();
  await ensureActiveConversation();
  await Promise.all([loadHistory(), loadDocuments(), loadMessageCaptures()]);
  showToast("会话已删除");
}
async function loadHistory() {
  if (!state.conversationId) {
    resetMessageCaptureState();
    clearConversation();
    return;
  }

  const query = new URLSearchParams({
    team_id: state.teamId,
    user_id: state.userId,
    conversation_id: state.conversationId,
    limit: "200",
  });
  const response = await apiRequest(`/chat/history?${query.toString()}`);
  state.history = Array.isArray(response.items) ? response.items : [];
  renderCurrentConversationMessages();
}

async function loadMessageCaptures() {
  const conversationId = state.conversationId;
  const spaceId = getActiveSpaceId();
  if (!ensureIdentity() || !state.conversationId || !spaceId) {
    resetMessageCaptureState();
    renderFavoriteWorkspace();
    return;
  }
  const requestSeq = state.messageCaptureRequestSeq + 1;
  state.messageCaptureRequestSeq = requestSeq;

  const query = new URLSearchParams({
    team_id: state.teamId,
    user_id: state.userId,
    space_id: spaceId,
  });
  const favorites = await apiRequest(`/favorites/answers?${query.toString()}`);
  if (
    requestSeq !== state.messageCaptureRequestSeq
    || conversationId !== state.conversationId
    || spaceId !== getActiveSpaceId()
  ) {
    return;
  }

  const nextState = createEmptyMessageCaptureState();

  for (const item of Array.isArray(favorites) ? favorites : []) {
    if (item?.message_id) {
      nextState.favoritesByMessageId[item.message_id] = item;
    }
  }

  setFavoriteItems(Array.isArray(favorites) ? favorites : []);
  state.messageCaptures = nextState;
  state.favoriteWorkspaceAssets = createEmptyFavoriteWorkspaceAssetState();
  if (state.history.length) {
    renderCurrentConversationMessages({ scrollToEnd: false });
  }
  renderFavoriteWorkspace();
  if (state.workspaceView === WORKSPACE_VIEW_FAVORITES && state.favoriteItems.length) {
    await loadFavoriteWorkspaceAssets();
  }
}

async function loadFavoriteWorkspaceAssets() {
  const conversationId = state.conversationId;
  const spaceId = getActiveSpaceId();
  if (!ensureIdentity() || !state.conversationId || !spaceId || !state.favoriteItems.length) {
    state.favoriteWorkspaceAssets = createEmptyFavoriteWorkspaceAssetState();
    renderFavoriteWorkspace();
    return;
  }

  const requestSeq = state.favoriteWorkspaceRequestSeq + 1;
  state.favoriteWorkspaceRequestSeq = requestSeq;

  const query = new URLSearchParams({
    team_id: state.teamId,
    user_id: state.userId,
    space_id: spaceId,
  });
  const [memories, conclusions, libraryDocs] = await Promise.all([
    apiRequest(`/memory/cards?${query.toString()}`),
    apiRequest(`/conclusions?${query.toString()}`),
    apiRequest(`/library/documents?${query.toString()}`),
  ]);

  if (
    requestSeq !== state.favoriteWorkspaceRequestSeq
    || conversationId !== state.conversationId
    || spaceId !== getActiveSpaceId()
  ) {
    return;
  }

  const nextAssets = createEmptyFavoriteWorkspaceAssetState();

  for (const item of Array.isArray(memories) ? memories : []) {
    if (item?.source_message_id) {
      nextAssets.memoriesByMessageId[item.source_message_id] = item;
    }
  }

  for (const item of Array.isArray(conclusions) ? conclusions : []) {
    if (normalizeStatus(item?.status) === "archived") {
      continue;
    }
    if (item?.source_message_id) {
      nextAssets.conclusionsByMessageId[item.source_message_id] = item;
    }
  }

  for (const item of Array.isArray(libraryDocs) ? libraryDocs : []) {
    const meta = safeParseJson(item?.meta_json, {});
    const sourceMessageId = typeof meta?.source_message_id === "string" ? meta.source_message_id : "";
    if (!sourceMessageId) {
      continue;
    }
    if (String(item?.asset_kind || "").trim().toLowerCase() !== "knowledge_doc") {
      continue;
    }
    nextAssets.libraryDocsByMessageId[sourceMessageId] = item;
  }

  state.favoriteWorkspaceAssets = nextAssets;
  renderFavoriteWorkspace();
}

function renderCurrentConversationMessages(options = {}) {
  const shouldScrollToEnd = options.scrollToEnd !== false;
  const previousScrollTop = els.conversation?.scrollTop ?? 0;
  clearConversation();
  if (!state.history.length) {
    refreshWorkspaceUi();
    return;
  }

  const ordered = [...state.history].reverse();
  const latestMessageId = state.history[0]?.message_id || "";
  for (const item of ordered) {
    const allowLatestEdit = item.message_id === latestMessageId && !["action", "agent"].includes(item.channel);
    appendMessage("user", item.request_text || "", {
      createdAt: item.created_at,
      messageId: item.message_id,
      channel: item.channel,
      editable: allowLatestEdit,
      deletable: true,
      autoScroll: false,
    });

    const responsePayload = item.response_payload || {};
    appendMessage("assistant", item.response_text || "", {
      createdAt: item.created_at,
      contentParts: normalizeResponseContentParts(responsePayload),
      sources: normalizeResponseSources(responsePayload),
      mode: responsePayload.mode || "",
      model: responsePayload.model || "",
      agentRunId: responsePayload.run_id || "",
      agentStatus: responsePayload.status || "",
      agentSteps: Array.isArray(responsePayload.steps) ? responsePayload.steps : [],
      requiredConfirmations: Array.isArray(responsePayload.required_confirmations)
        ? responsePayload.required_confirmations
        : [],
      messageId: item.message_id,
      spaceId: item.space_id,
      requestText: item.request_text || "",
      channel: item.channel,
      regenerable: allowLatestEdit,
      autoScroll: false,
    });
  }
  refreshWorkspaceUi();
  if (shouldScrollToEnd) {
    scrollToBottom();
    return;
  }
  if (els.conversation) {
    els.conversation.scrollTop = previousScrollTop;
  }
}

function buildFavoriteWorkspaceContext(favorite) {
  return {
    messageId: favorite?.message_id || "",
    spaceId: favorite?.space_id || getActiveSpaceId(),
    requestText: favorite?.question_text || "",
    answerText: favorite?.answer_text || "",
    createdAt: favorite?.created_at || null,
    sources: safeParseJson(favorite?.sources_json, []),
  };
}

function appendFavoriteWorkspaceEmptyState(text) {
  if (!els.favoriteList) {
    return;
  }
  const empty = document.createElement("article");
  empty.className = "favorite-empty-state";
  empty.textContent = text;
  els.favoriteList.appendChild(empty);
}

function renderFavoriteWorkspace() {
  if (!els.favoriteList || !els.favoriteEmptyState || !els.favoriteDetailContent) {
    return;
  }

  els.favoriteList.innerHTML = "";

  if (!ensureIdentity()) {
    state.selectedFavoriteId = "";
    appendFavoriteWorkspaceEmptyState("设置工作台后可把回答收藏到这里，再继续整理为长期记忆、结论和资料库。");
    renderFavoriteDetailPane();
    refreshWorkspaceUi();
    return;
  }

  if (!state.favoriteItems.length) {
    state.selectedFavoriteId = "";
    appendFavoriteWorkspaceEmptyState("当前会话空间还没有收藏内容。先在回答卡片里点“收藏”，再回来整理。");
    renderFavoriteDetailPane();
    refreshWorkspaceUi();
    return;
  }

  if (!state.favoriteItems.some((item) => item.favorite_id === state.selectedFavoriteId)) {
    state.selectedFavoriteId = state.favoriteItems[0]?.favorite_id || "";
  }

  for (const favorite of state.favoriteItems) {
    const favoriteContext = buildFavoriteWorkspaceContext(favorite);
    const memoryRecord = getFavoriteWorkspaceAsset("memoriesByMessageId", favorite.message_id);
    const conclusionRecord = getFavoriteWorkspaceAsset("conclusionsByMessageId", favorite.message_id);
    const libraryRecord = getFavoriteWorkspaceAsset("libraryDocsByMessageId", favorite.message_id);

    const card = document.createElement("article");
    card.className = "favorite-card favorite-list-item";
    card.tabIndex = 0;
    card.classList.toggle("active", favorite.favorite_id === state.selectedFavoriteId);
    card.addEventListener("click", () => {
      setSelectedFavoriteId(favorite.favorite_id);
    });

    const head = document.createElement("div");
    head.className = "favorite-card-head";

    const heading = document.createElement("div");
    heading.className = "favorite-card-heading";

    const title = document.createElement("h3");
    title.className = "favorite-card-title";
    title.textContent = favorite.title || buildCaptureTitle(favorite.question_text || favorite.answer_text, "收藏回答");

    const meta = document.createElement("div");
    meta.className = "favorite-card-meta";
    meta.textContent = [
      favorite.conversation_id ? "来自当前会话" : "",
      formatTime(favorite.created_at),
    ].filter(Boolean).join(" · ");

    heading.append(title, meta);
    head.appendChild(heading);

    const question = document.createElement("div");
    question.className = "favorite-card-question";
    question.textContent = favorite.question_text || "未记录问题";

    const answer = document.createElement("div");
    answer.className = "favorite-card-answer";
    answer.textContent = favorite.answer_text || "未记录回答";

    const actions = document.createElement("div");
    actions.className = "favorite-card-actions";

    actions.appendChild(createMessageActionButton(
      memoryRecord ? "取消记忆" : "设为长期记忆",
      () => {
        const task = memoryRecord
          ? removeFavoriteMemory(favorite, memoryRecord)
          : promoteFavoriteToMemory(favorite);
        task.catch((error) => showToast(error.message, true));
      },
      {
        active: Boolean(memoryRecord),
        disabled: isCaptureActionPending(favorite.message_id, "memory"),
        title: memoryRecord ? "把这条收藏从长期记忆中移除" : "把这条收藏转为长期记忆",
        pressed: Boolean(memoryRecord),
      },
    ));

    actions.appendChild(createMessageActionButton(
      conclusionRecord ? "取消结论" : "沉淀为结论",
      () => {
        const task = conclusionRecord
          ? archiveFavoriteConclusion(favorite, conclusionRecord)
          : promoteFavoriteToConclusion(favorite);
        task.catch((error) => showToast(error.message, true));
      },
      {
        active: Boolean(conclusionRecord),
        disabled: isCaptureActionPending(favorite.message_id, "conclusion"),
        title: conclusionRecord ? "撤回这条收藏的结论沉淀" : "把这条收藏沉淀为结论",
        pressed: Boolean(conclusionRecord),
      },
    ));

    actions.appendChild(createMessageActionButton(
      libraryRecord ? "移出资料库" : "发布到资料库",
      () => {
        const task = libraryRecord
          ? removeFavoriteLibraryDocument(favorite, libraryRecord)
          : publishFavoriteToLibrary(favorite);
        task.catch((error) => showToast(error.message, true));
      },
      {
        active: Boolean(libraryRecord),
        disabled: isCaptureActionPending(favorite.message_id, "library"),
        title: libraryRecord ? "把这条收藏从资料库中移除" : "把这条收藏发布到资料库",
        pressed: Boolean(libraryRecord),
      },
    ));

    actions.appendChild(createMessageActionButton("复制回答", () => {
      copyMessageText(favorite.answer_text || "").catch((error) => showToast(error.message, true));
    }));

    actions.appendChild(createMessageActionButton("回到对话", () => {
      setWorkspaceView(WORKSPACE_VIEW_CHAT)
        .then(() => focusMessageInConversation(favorite.message_id))
        .catch((error) => showToast(error.message, true));
    }));

    actions.appendChild(createMessageActionButton(
      "取消收藏",
      () => {
        toggleAssistantFavorite(favoriteContext).catch((error) => showToast(error.message, true));
      },
      {
        disabled: isCaptureActionPending(favorite.message_id, "favorite"),
        title: "把这条回答从收藏夹移除",
      },
    ));

    card.append(head, question, answer, actions);
    els.favoriteList.appendChild(card);
  }

  renderFavoriteDetailPane();
  refreshWorkspaceUi();
}

function renderFavoriteDetailPane() {
  const detailEl = els.favoriteDetailContent;
  const emptyEl = els.favoriteEmptyState;
  if (!detailEl || !emptyEl) {
    return;
  }

  const selected = state.favoriteItems.find((item) => item.favorite_id === state.selectedFavoriteId);
  if (!selected) {
    emptyEl.classList.remove("hidden");
    detailEl.classList.add("hidden");
    detailEl.innerHTML = "";
    return;
  }

  emptyEl.classList.add("hidden");
  detailEl.classList.remove("hidden");
  detailEl.innerHTML = `
    <article class="favorite-detail-card">
      <h3>${escapeHtml(selected.title || buildCaptureTitle(selected.question_text || selected.answer_text, "收藏回答"))}</h3>
      <p class="favorite-detail-meta">${escapeHtml(formatTime(selected.created_at))}</p>
      <div class="favorite-card-question">${escapeHtml(selected.question_text || "未记录问题")}</div>
      <div class="favorite-card-answer">${escapeHtml(selected.answer_text || "未记录回答")}</div>
      <div class="favorite-detail-actions">
        <button class="message-icon-btn" type="button" data-favorite-action="memory">Save to memory</button>
        <button class="message-icon-btn" type="button" data-favorite-action="conclusion">Turn into conclusion</button>
        <button class="message-icon-btn" type="button" data-favorite-action="library">Add to library</button>
      </div>
    </article>
  `;
}

function focusMessageInConversation(messageId) {
  if (!messageId || !els.messageList) {
    return;
  }
  const target = els.messageList.querySelector(`[data-message-id="${messageId}"]`);
  if (target instanceof HTMLElement) {
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

async function deleteHistoryMessage(messageId) {
  if (!messageId) {
    return;
  }
  if (!window.confirm("确认删除这一轮对话吗？")) {
    return;
  }

  const query = new URLSearchParams({
    team_id: state.teamId,
    user_id: state.userId,
    conversation_id: state.conversationId,
  });
  await apiRequest(`/chat/history/${encodeURIComponent(messageId)}?${query.toString()}`, {
    method: "DELETE",
  });
  await loadHistory();
  showToast("消息已删除");
}

async function editHistoryMessage(messageId, currentText, channel) {
  if (!messageId) {
    return;
  }
  if (channel === "action" || channel === "agent") {
    showToast("工具调用和 Agent 消息不支持编辑", true);
    return;
  }

  const input = window.prompt("编辑用户消息", currentText || "");
  if (input === null) {
    return;
  }
  const requestText = input.trim();
  if (!requestText) {
    showToast("消息不能为空", true);
    return;
  }
  if (requestText === (currentText || "").trim()) {
    return;
  }

  await apiRequest(`/chat/history/${encodeURIComponent(messageId)}`, {
    method: "PUT",
    body: {
      team_id: state.teamId,
      user_id: state.userId,
      request_text: requestText,
    },
  });
  await loadHistory();
  showToast("消息已更新并重新生成回复");
}

async function copyMessageText(content) {
  const normalized = String(content || "").trim();
  if (!normalized) {
    return;
  }

  try {
    await navigator.clipboard.writeText(normalized);
    showToast("已复制回复");
  } catch {
    showToast("复制失败，请检查浏览器权限", true);
  }
}

async function regenerateAssistantMessage(messageId, requestText, channel) {
  if (!messageId || !requestText) {
    return;
  }
  if (channel === "action" || channel === "agent") {
    showToast("工具调用和 Agent 消息不支持重新生成", true);
    return;
  }

  const availableModels = dedupeStrings([
    DEFAULT_MODEL_ID,
    NONE_MODEL_ID,
    ...state.modelConfigs.map((item) => item.model_name),
  ]);
  const selected = window.prompt(`选择重新生成模型：${availableModels.join(", ")}`, state.selectedModel || DEFAULT_MODEL_ID);
  if (selected === null) {
    return;
  }

  const normalizedModel = selected.trim() || state.selectedModel || DEFAULT_MODEL_ID;
  if (!availableModels.includes(normalizedModel)) {
    showToast("模型不存在，请先在模型列表中添加", true);
    return;
  }

  const payload = {
    team_id: state.teamId,
    user_id: state.userId,
    request_text: requestText,
    embedding_model: state.selectedEmbedding || DEFAULT_EMBEDDING_ID,
  };
  if (normalizedModel !== DEFAULT_MODEL_ID) {
    payload.model = normalizedModel;
  }

  await apiRequest(`/chat/history/${encodeURIComponent(messageId)}`, {
    method: "PUT",
    body: payload,
  });

  state.selectedModel = normalizedModel;
  persistSelectedModel();
  initModelOptions();
  await loadHistory();
  showToast(`已使用 ${normalizedModel} 重新生成`);
}

function truncateText(value, maxLength) {
  const normalized = String(value || "").trim();
  if (!normalized || normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(1, maxLength - 1)).trim()}…`;
}

function buildCaptureTitle(requestText, fallbackPrefix) {
  const normalized = truncateText(requestText, 96);
  if (normalized) {
    return normalized;
  }
  return fallbackPrefix;
}

function buildConclusionTitle(requestText) {
  const normalized = truncateText(requestText, 84);
  if (!normalized) {
    return "聊天结论";
  }
  return truncateText(`关于「${normalized}」的结论`, 128);
}

function safeParseJson(raw, fallback) {
  if (!raw || typeof raw !== "string") {
    return fallback;
  }
  try {
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function buildSourceLocator(source) {
  if (!source || typeof source !== "object") {
    return "";
  }
  if (source.locator_label) {
    return String(source.locator_label);
  }
  if (Number.isInteger(Number(source.page_no))) {
    return `Page ${Number(source.page_no)}`;
  }
  if (Number.isInteger(Number(source.chunk_index))) {
    return `Chunk ${Number(source.chunk_index) + 1}`;
  }
  return "";
}

function buildCaptureEvidence(sources) {
  if (!Array.isArray(sources) || !sources.length) {
    return undefined;
  }
  return {
    sources: sources.map((source) => ({
      document_id: source.document_id || "",
      source_name: source.source_name || "",
      locator: buildSourceLocator(source),
      snippet: truncateText(source.snippet || "", 280),
      score: Number(source.score || 0),
    })),
  };
}

function buildLibraryCaptureMarkdownFromContext(context) {
  const questionText = truncateText(context?.requestText || "未记录问题", 8_000) || "未记录问题";
  const answerText = truncateText(context?.answerText || "", 180_000);
  const lines = [
    `# ${buildCaptureTitle(context?.requestText || context?.answerText, "聊天回答沉淀")}`,
    "",
    `- 来源消息: ${context?.messageId || "unknown"}`,
    `- 会话: ${state.conversationId || "unknown"}`,
    `- 生成时间: ${context?.createdAt || new Date().toISOString()}`,
    "",
    "## 用户问题",
    "",
    questionText,
    "",
    "## 回答内容",
    "",
    answerText,
  ];

  const sources = Array.isArray(context?.sources) ? context.sources : [];
  if (sources.length) {
    lines.push("", "## 引用来源", "");
    for (const source of sources) {
      const prefix = source.source_name || "未命名资料";
      const locator = buildSourceLocator(source);
      const summary = [prefix, locator].filter(Boolean).join(" · ");
      lines.push(`- ${summary || prefix}`);
      if (source.snippet) {
        lines.push(`  - 摘要：${truncateText(source.snippet, 280)}`);
      }
    }
  }

  return lines.join("\n").trim();
}

async function runMessageCaptureAction(messageId, action, task) {
  if (isCaptureActionPending(messageId, action)) {
    return null;
  }

  setCaptureActionPending(messageId, action, true);
  renderCurrentConversationMessages({ scrollToEnd: false });
  renderFavoriteWorkspace();
  try {
    return await task();
  } finally {
    setCaptureActionPending(messageId, action, false);
    renderCurrentConversationMessages({ scrollToEnd: false });
    renderFavoriteWorkspace();
  }
}

async function captureAssistantFavorite(context) {
  const existing = getMessageCaptureRecord("favoritesByMessageId", context.messageId);
  if (existing) {
    showToast("这条回答已经收藏过了");
    return existing;
  }

  return runMessageCaptureAction(context.messageId, "favorite", async () => {
    const created = await apiRequest("/favorites/answers", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        space_id: context.spaceId,
        message_id: context.messageId,
      },
    });
    updateMessageCaptureRecord("favoritesByMessageId", context.messageId, created);
    upsertFavoriteItem(created);
    showToast("已收藏这条回答");
    return created;
  });
}

async function captureAssistantMemory(context) {
  const existing = getMessageCaptureRecord("memoriesByMessageId", context.messageId);
  if (existing) {
    showToast("这条回答已经记住过了");
    return existing;
  }

  return runMessageCaptureAction(context.messageId, "memory", async () => {
    const created = await apiRequest("/memory/cards", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        space_id: context.spaceId,
        category: "assistant_answer",
        title: buildCaptureTitle(context.requestText || context.answerText, "聊天记忆"),
        content: truncateText(context.answerText || "", 4000) || "未记录回答",
        summary: truncateText(context.requestText || "", 200),
        source_message_id: context.messageId,
      },
    });
    updateMessageCaptureRecord("memoriesByMessageId", context.messageId, created);
    showToast("已记住这条回答");
    return created;
  });
}

async function captureAssistantConclusion(context) {
  const existing = getMessageCaptureRecord("conclusionsByMessageId", context.messageId);
  if (existing) {
    showToast("这条回答已经沉淀为结论");
    return existing;
  }

  return runMessageCaptureAction(context.messageId, "conclusion", async () => {
    const created = await apiRequest("/conclusions", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        space_id: context.spaceId,
        title: buildConclusionTitle(context.requestText || context.answerText),
        topic: truncateText(context.requestText || "", 128),
        content: truncateText(context.answerText || "", 12000) || "未记录回答",
        summary: truncateText(context.answerText || "", 360),
        source_message_id: context.messageId,
        evidence: buildCaptureEvidence(context.sources),
      },
    });
    updateMessageCaptureRecord("conclusionsByMessageId", context.messageId, created);
    showToast("已沉淀为结论");
    return created;
  });
}

async function captureAssistantLibrary(context) {
  const existing = getMessageCaptureRecord("libraryDocsByMessageId", context.messageId);
  if (existing) {
    showToast("这条回答已经发布到资料库");
    return existing;
  }

  return runMessageCaptureAction(context.messageId, "library", async () => {
    const created = await apiRequest("/documents/import", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        space_id: context.spaceId,
        source_name: `answer-${context.messageId}.md`,
        content_type: "md",
        content: buildLibraryCaptureMarkdownFromContext(context),
        auto_index: true,
        meta: {
          source_message_id: context.messageId,
          capture_kind: "assistant_answer",
          source_conversation_id: state.conversationId || "",
        },
      },
    });
    updateMessageCaptureRecord("libraryDocsByMessageId", context.messageId, created);
    showToast("已发布到资料库");
    return created;
  });
}

async function toggleAssistantFavorite(context) {
  const existing = getMessageCaptureRecord("favoritesByMessageId", context.messageId);
  if (!existing) {
    return captureAssistantFavorite(context);
  }

  return runMessageCaptureAction(context.messageId, "favorite", async () => {
    const query = new URLSearchParams({
      team_id: state.teamId,
      user_id: state.userId,
    });
    await apiRequest(`/favorites/answers/${encodeURIComponent(existing.favorite_id)}?${query.toString()}`, {
      method: "DELETE",
    });
    removeMessageCaptureRecord("favoritesByMessageId", context.messageId);
    removeFavoriteItem(existing.favorite_id);
    removeFavoriteWorkspaceAssets(context.messageId);
    renderFavoriteWorkspace();
    showToast("已取消收藏");
    return null;
  });
}

async function promoteFavoriteToMemory(favorite) {
  const existing = getFavoriteWorkspaceAsset("memoriesByMessageId", favorite.message_id);
  if (existing) {
    showToast("这条收藏已经转为长期记忆");
    return existing;
  }

  return runMessageCaptureAction(favorite.message_id, "memory", async () => {
    const result = await apiRequest(`/favorites/answers/${encodeURIComponent(favorite.favorite_id)}/promote-to-memory`, {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        space_id: favorite.space_id,
        category: "assistant_answer",
        title: favorite.title || buildCaptureTitle(favorite.question_text || favorite.answer_text, "聊天记忆"),
        summary: truncateText(favorite.question_text || "", 200),
      },
    });
    if (result?.favorite) {
      upsertFavoriteItem(result.favorite);
    }
    await loadFavoriteWorkspaceAssets();
    showToast("已转为长期记忆");
    return getFavoriteWorkspaceAsset("memoriesByMessageId", favorite.message_id);
  });
}

async function removeFavoriteMemory(favorite, memoryRecord) {
  const existing = memoryRecord || getFavoriteWorkspaceAsset("memoriesByMessageId", favorite.message_id);
  if (!existing?.memory_id) {
    showToast("这条收藏还没有转为长期记忆");
    return null;
  }

  return runMessageCaptureAction(favorite.message_id, "memory", async () => {
    await apiRequest(`/memory/cards/${encodeURIComponent(existing.memory_id)}`, {
      method: "DELETE",
    });
    removeFavoriteWorkspaceAsset("memoriesByMessageId", favorite.message_id);
    renderFavoriteWorkspace();
    showToast("已取消长期记忆");
    return null;
  });
}

async function promoteFavoriteToConclusion(favorite) {
  const existing = getFavoriteWorkspaceAsset("conclusionsByMessageId", favorite.message_id);
  if (existing) {
    showToast("这条收藏已经沉淀为结论");
    return existing;
  }

  return runMessageCaptureAction(favorite.message_id, "conclusion", async () => {
    const result = await apiRequest(`/favorites/answers/${encodeURIComponent(favorite.favorite_id)}/promote-to-conclusion`, {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        space_id: favorite.space_id,
        title: buildConclusionTitle(favorite.question_text || favorite.answer_text),
        topic: truncateText(favorite.question_text || "", 128),
        summary: truncateText(favorite.answer_text || "", 360),
      },
    });
    if (result?.favorite) {
      upsertFavoriteItem(result.favorite);
    }
    await loadFavoriteWorkspaceAssets();
    showToast("已沉淀为结论");
    return getFavoriteWorkspaceAsset("conclusionsByMessageId", favorite.message_id);
  });
}

async function archiveFavoriteConclusion(favorite, conclusionRecord) {
  const existing = conclusionRecord || getFavoriteWorkspaceAsset("conclusionsByMessageId", favorite.message_id);
  if (!existing?.conclusion_id) {
    showToast("这条收藏还没有沉淀为结论");
    return null;
  }

  return runMessageCaptureAction(favorite.message_id, "conclusion", async () => {
    await apiRequest(`/conclusions/${encodeURIComponent(existing.conclusion_id)}/archive`, {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
      },
    });
    removeFavoriteWorkspaceAsset("conclusionsByMessageId", favorite.message_id);
    renderFavoriteWorkspace();
    showToast("已取消结论沉淀");
    return null;
  });
}

async function publishFavoriteToLibrary(favorite) {
  const existing = getFavoriteWorkspaceAsset("libraryDocsByMessageId", favorite.message_id);
  if (existing) {
    showToast("这条收藏已经发布到资料库");
    return existing;
  }

  return runMessageCaptureAction(favorite.message_id, "library", async () => {
    const favoriteContext = buildFavoriteWorkspaceContext(favorite);
    const created = await apiRequest("/documents/import", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        space_id: favorite.space_id,
        source_name: `favorite-${favorite.message_id}.md`,
        content_type: "md",
        content: buildLibraryCaptureMarkdownFromContext(favoriteContext),
        auto_index: true,
        meta: {
          source_message_id: favorite.message_id,
          capture_kind: "favorite_answer",
          source_conversation_id: favorite.conversation_id || state.conversationId || "",
          favorite_id: favorite.favorite_id,
        },
      },
    });
    updateFavoriteWorkspaceAsset("libraryDocsByMessageId", favorite.message_id, created);
    renderFavoriteWorkspace();
    showToast("已发布到资料库");
    return created;
  });
}

async function removeFavoriteLibraryDocument(favorite, libraryRecord) {
  const existing = libraryRecord || getFavoriteWorkspaceAsset("libraryDocsByMessageId", favorite.message_id);
  if (!existing?.document_id) {
    showToast("这条收藏还没有发布到资料库");
    return null;
  }

  return runMessageCaptureAction(favorite.message_id, "library", async () => {
    const query = new URLSearchParams();
    if (existing.conversation_id) {
      query.set("conversation_id", existing.conversation_id);
    }
    const requestPath = query.toString()
      ? `/documents/${encodeURIComponent(existing.document_id)}?${query.toString()}`
      : `/documents/${encodeURIComponent(existing.document_id)}`;
    await apiRequest(requestPath, {
      method: "DELETE",
    });
    removeFavoriteWorkspaceAsset("libraryDocsByMessageId", favorite.message_id);
    renderFavoriteWorkspace();
    showToast("已移出资料库");
    return null;
  });
}

async function loadDocuments() {
  clearSurfaceStatus(els.fileDrawerStatus);
  if (!state.conversationId) {
    state.documents = [];
    state.selectedDocumentIds = [];
    syncAutoChatMode();
    resetMessageCaptureState();
    renderDocuments();
    return;
  }

  try {
    const query = new URLSearchParams({
      team_id: state.teamId,
      conversation_id: state.conversationId,
    });
    const response = await apiRequest(`/documents?${query.toString()}`);
    state.documents = Array.isArray(response) ? response : [];
    state.selectedDocumentIds = state.selectedDocumentIds.filter((documentId) => {
      return state.documents.some((doc) => doc.document_id === documentId && normalizeStatus(doc.status) === "ready");
    });
    syncAutoChatMode();
    renderDocuments();
  } catch (error) {
    setSurfaceStatus(els.fileDrawerStatus, error.message, true);
    throw error;
  }
}

function renderDocuments() {
  els.documentList.innerHTML = "";

  if (!state.documents.length) {
    appendEmpty(els.documentList, "还没有导入文件");
    refreshComposerChrome();
    refreshWorkspaceUi();
    return;
  }

  for (const doc of state.documents) {
    const li = document.createElement("li");
    li.className = `document-item ${normalizeStatus(doc.status)}`;

    const head = document.createElement("div");
    head.className = "doc-card-head";

    const titleBtn = document.createElement("button");
    titleBtn.type = "button";
    titleBtn.className = "doc-card-action";
    titleBtn.textContent = doc.source_name;
    titleBtn.addEventListener("click", () => {
      openDocumentPreview(doc).catch((error) => showToast(error.message, true));
    });

    const statusBadge = document.createElement("span");
    statusBadge.className = `doc-status ${normalizeStatus(doc.status)}`;
    statusBadge.textContent = formatStatusLabel(doc.status);
    head.append(titleBtn, statusBadge);

    const meta = document.createElement("div");
    meta.className = "doc-meta";
    meta.textContent = `${formatContentTypeLabel(doc.content_type)} · ${formatBytes(doc.size_bytes)} · ${formatTime(doc.created_at)}`;

    const actions = document.createElement("div");
    actions.className = "doc-card-actions";

    const previewBtn = document.createElement("button");
    previewBtn.type = "button";
    previewBtn.className = "doc-card-action";
    previewBtn.textContent = "预览";
    previewBtn.addEventListener("click", () => {
      openDocumentPreview(doc).catch((error) => showToast(error.message, true));
    });

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "doc-card-action danger";
    deleteBtn.textContent = "删除";
    deleteBtn.addEventListener("click", () => {
      deleteDocument(doc.document_id, doc.source_name).catch((error) => showToast(error.message, true));
    });

    actions.append(previewBtn, deleteBtn);
    li.append(head, meta);

    const normalizedStatus = normalizeStatus(doc.status);
    if (normalizedStatus === "failed" && (doc.error_message || doc.error_code)) {
      const note = document.createElement("div");
      note.className = "doc-note error";
      note.textContent = doc.error_message || doc.error_code;
      li.appendChild(note);
    } else if (normalizedStatus !== "ready" && normalizedStatus !== "deleted") {
      const note = document.createElement("div");
      note.className = "doc-note";
      note.textContent = "处理中，完成后会自动加入检索范围。";
      li.appendChild(note);
    }

    li.appendChild(actions);
    els.documentList.appendChild(li);
  }
  refreshComposerChrome();
  refreshWorkspaceUi();
}

function renderComposerContextRow() {
  if (!els.composerContextRow) {
    return;
  }

  els.composerContextRow.innerHTML = "";

  const readyCount = getReadyDocumentCount();
  const processingCount = getProcessingDocumentCount();
  const selectedAgentApp = getSelectedAgentApp();
  const chips = [
    {
      label: state.agentMode
        ? (selectedAgentApp ? `应用 · ${selectedAgentApp.name}` : "Agent执行")
        : (state.chatMode === CHAT_MODE_DOCS && readyCount ? "资料增强" : "直接聊天"),
      tone: state.agentMode || state.chatMode === CHAT_MODE_DOCS ? "accent" : "neutral",
    },
    {
      label: state.documents.length ? `${state.documents.length} 文档` : "暂无文档",
      tone: state.documents.length ? "info" : "muted",
    },
  ];

  if (readyCount) {
    chips.push({
      label: `${readyCount} ready`,
      tone: "success",
    });
  } else if (processingCount) {
    chips.push({
      label: `${processingCount} 处理中`,
      tone: "warning",
    });
  }

  chips.push({
    label: state.selectedDocumentIds.length
      ? `${state.selectedDocumentIds.length} 已选`
      : (state.chatMode === CHAT_MODE_DOCS ? "全量检索" : "未选文档"),
    tone: state.selectedDocumentIds.length ? "accent" : "neutral",
  });

  for (const chip of chips) {
    const span = document.createElement("span");
    span.className = `composer-context-chip ${chip.tone}`;
    span.textContent = chip.label;
    els.composerContextRow.appendChild(span);
  }

  const filesAction = document.createElement("button");
  filesAction.id = "composerFilesBtn";
  filesAction.type = "button";
  filesAction.className = `composer-context-action${state.activeSurface === ACTIVE_SURFACE_FILES ? " active" : ""}${state.documents.length ? "" : " is-empty"}`;
  filesAction.setAttribute("aria-pressed", String(state.activeSurface === ACTIVE_SURFACE_FILES));
  filesAction.setAttribute(
    "aria-label",
    state.documents.length
      ? `打开本会话文件，当前 ${state.documents.length} 个`
      : "添加文件到当前会话",
  );

  const filesActionLabel = document.createElement("span");
  filesActionLabel.className = "composer-context-action-label";
  filesActionLabel.textContent = "文件";
  filesAction.appendChild(filesActionLabel);

  const filesActionCount = document.createElement("span");
  filesActionCount.className = "composer-context-action-count";
  filesActionCount.textContent = state.documents.length ? String(state.documents.length) : "添加";
  filesAction.appendChild(filesActionCount);

  filesAction.addEventListener("click", () => {
    if (!state.documents.length) {
      closeAttachMenu();
      els.fileInput.click();
      return;
    }
    const nextSurface = state.activeSurface === ACTIVE_SURFACE_FILES
      ? ACTIVE_SURFACE_NONE
      : ACTIVE_SURFACE_FILES;
    setActiveSurface(nextSurface);
    refreshWorkspaceUi();
  });
  els.composerContextRow.appendChild(filesAction);
}

function refreshComposerChrome() {
  renderAttachmentStrip();
  renderComposerContextRow();
}

function renderAttachmentStrip() {
  els.attachmentStrip.innerHTML = "";

  if (!state.documents.length) {
    els.attachmentStrip.classList.add("hidden");
    return;
  }

  els.attachmentStrip.classList.remove("hidden");

  const list = document.createElement("div");
  list.className = "attachment-list";

  for (const doc of state.documents) {
    const chip = document.createElement("div");
    chip.className = `attachment-chip${state.selectedDocumentIds.includes(doc.document_id) ? " active" : ""}`;

    const mainBtn = document.createElement("button");
    mainBtn.type = "button";
    mainBtn.className = "doc-chip-main";
    mainBtn.title = normalizeStatus(doc.status) === "ready"
      ? `单击选择，双击预览 ${doc.source_name}`
      : `双击预览 ${doc.source_name}`;

    let clickTimer = null;
    mainBtn.addEventListener("click", () => {
      if (clickTimer !== null) {
        window.clearTimeout(clickTimer);
      }
      clickTimer = window.setTimeout(() => {
        clickTimer = null;
        handleChipClick(doc);
      }, 220);
    });
    mainBtn.addEventListener("dblclick", (event) => {
      event.preventDefault();
      if (clickTimer !== null) {
        window.clearTimeout(clickTimer);
        clickTimer = null;
      }
      openDocumentPreview(doc).catch((error) => showToast(error.message, true));
    });

    const name = document.createElement("span");
    name.className = "doc-chip-name";
    name.textContent = doc.source_name;

    const status = document.createElement("span");
    status.className = `doc-status ${normalizeStatus(doc.status)}`;
    status.textContent = formatStatusLabel(doc.status);

    mainBtn.append(name, status);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "doc-chip-delete";
    deleteBtn.textContent = "×";
    deleteBtn.title = `删除 ${doc.source_name}`;
    deleteBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteDocument(doc.document_id, doc.source_name).catch((error) => showToast(error.message, true));
    });

    chip.append(mainBtn, deleteBtn);
    list.appendChild(chip);
  }

  els.attachmentStrip.appendChild(list);
}

function handleChipClick(doc) {
  if (normalizeStatus(doc.status) !== "ready") {
    openDocumentPreview(doc).catch((error) => showToast(error.message, true));
    return;
  }
  toggleDocumentSelection(doc.document_id);
}

function openDocumentPreview(doc) {
  return openSourcePreview({
    document_id: doc.document_id,
    source_name: doc.source_name,
    chunk_index: null,
    snippet: null,
  });
}

function toggleDocumentSelection(documentId) {
  if (state.selectedDocumentIds.includes(documentId)) {
    state.selectedDocumentIds = state.selectedDocumentIds.filter((item) => item !== documentId);
  } else {
    state.selectedDocumentIds = [...state.selectedDocumentIds, documentId];
  }
  syncAutoChatMode();
  refreshComposerChrome();
  refreshWorkspaceUi();
}

async function deleteDocument(documentId, sourceName) {
  if (!window.confirm(`确认删除文件“${sourceName}”吗？`)) {
    return;
  }

  const query = new URLSearchParams({
    team_id: state.teamId,
    conversation_id: state.conversationId,
  });
  await apiRequest(`/documents/${encodeURIComponent(documentId)}?${query.toString()}`, {
    method: "DELETE",
  });

  state.selectedDocumentIds = state.selectedDocumentIds.filter((item) => item !== documentId);
  closePreviewDrawer();
  await loadDocuments();
  showToast(`已删除文件：${sourceName}`);
}

function resetPreviewDrawer() {
  if (els.previewEyebrow) {
    els.previewEyebrow.textContent = "媒体预览";
  }
  els.previewTitle.textContent = "文件预览";
  els.previewMeta.innerHTML = "";
  els.previewSnippet.classList.add("hidden");
  els.previewSnippet.textContent = "";
  els.previewMedia.innerHTML = "";
  els.previewMedia.classList.add("hidden");
  els.previewContent.textContent = "";
  els.previewContent.classList.add("hidden");
  setPreviewDownloadButton(null);
}

function openPreviewDrawer() {
  els.previewDrawer.classList.remove("hidden");
  els.previewDrawer.setAttribute("aria-hidden", "false");
}

function appendPreviewMetaLine(text) {
  if (!text) {
    return;
  }
  const div = document.createElement("div");
  div.textContent = text;
  els.previewMeta.appendChild(div);
}

function appendPreviewMetaLink({ href, text, download = "" }) {
  if (!href || !text) {
    return;
  }
  const link = document.createElement("a");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = text;
  if (download) {
    link.download = download;
  }
  els.previewMeta.appendChild(link);
}

function setPreviewDownloadButton(config) {
  if (!els.downloadPreviewBtn) {
    return;
  }
  if (!config || !config.url) {
    els.downloadPreviewBtn.classList.add("hidden");
    els.downloadPreviewBtn.removeAttribute("href");
    els.downloadPreviewBtn.removeAttribute("download");
    return;
  }

  els.downloadPreviewBtn.href = config.url;
  els.downloadPreviewBtn.download = config.fileName || "image";
  els.downloadPreviewBtn.textContent = config.label || "下载图片";
  els.downloadPreviewBtn.classList.remove("hidden");
}

function showPreviewContent(text) {
  const normalized = String(text || "");
  if (!normalized) {
    els.previewContent.textContent = "";
    els.previewContent.classList.add("hidden");
    return;
  }
  els.previewContent.textContent = normalized;
  els.previewContent.classList.remove("hidden");
}

function buildImageDownloadName({ title, mimeType, url, fallback = "image-preview" }) {
  const normalizedTitle = String(title || "").trim().replace(/[\\/:*?"<>|]+/g, "_").replace(/\s+/g, "_");
  const baseName = normalizedTitle || fallback;
  const extension = imageExtensionFromMimeType(mimeType) || imageExtensionFromUrl(url) || "png";
  return `${baseName}.${extension}`;
}

function imageExtensionFromMimeType(mimeType) {
  const normalized = String(mimeType || "").trim().toLowerCase();
  if (normalized === "image/png") {
    return "png";
  }
  if (normalized === "image/jpeg") {
    return "jpg";
  }
  if (normalized === "image/webp") {
    return "webp";
  }
  if (normalized === "image/gif") {
    return "gif";
  }
  return "";
}

function imageExtensionFromUrl(url) {
  const normalized = String(url || "").trim();
  if (!normalized || normalized.startsWith("data:image/")) {
    return "";
  }
  const withoutQuery = normalized.split("?", 1)[0].toLowerCase();
  if (withoutQuery.endsWith(".png")) {
    return "png";
  }
  if (withoutQuery.endsWith(".jpg") || withoutQuery.endsWith(".jpeg")) {
    return "jpg";
  }
  if (withoutQuery.endsWith(".webp")) {
    return "webp";
  }
  if (withoutQuery.endsWith(".gif")) {
    return "gif";
  }
  return "";
}

function inferImageMimeType(url) {
  const normalized = String(url || "").trim();
  if (normalized.startsWith("data:image/")) {
    return normalized.slice("data:".length).split(";", 1)[0];
  }
  const ext = imageExtensionFromUrl(normalized);
  if (ext === "png") {
    return "image/png";
  }
  if (ext === "jpg") {
    return "image/jpeg";
  }
  if (ext === "webp") {
    return "image/webp";
  }
  if (ext === "gif") {
    return "image/gif";
  }
  return "";
}

function resetImageViewer() {
  if (!els.imageViewerTitle) {
    return;
  }
  els.imageViewerTitle.textContent = "图片预览";
  els.imageViewerMeta.textContent = "模型输出图片";
  els.imageViewerImg.src = "";
  els.imageViewerImg.alt = "";
  els.imageViewerCaption.textContent = "";
  els.imageViewerCaption.classList.add("hidden");
  els.downloadImageBtn.removeAttribute("href");
  els.downloadImageBtn.removeAttribute("download");
  els.openOriginalImageBtn.removeAttribute("href");
  els.openOriginalImageBtn.classList.add("hidden");
}

function openImageViewer() {
  if (!els.imageViewer) {
    return;
  }
  els.imageViewer.classList.remove("hidden");
  els.imageViewer.setAttribute("aria-hidden", "false");
}

function closeImageViewer() {
  if (!els.imageViewer) {
    return;
  }
  resetImageViewer();
  els.imageViewer.classList.add("hidden");
  els.imageViewer.setAttribute("aria-hidden", "true");
}

function openAssistantImagePreview(part) {
  if (!part || !part.url) {
    return;
  }

  closePreviewDrawer();
  resetImageViewer();
  els.imageViewerTitle.textContent = part.alt || "生成图片";
  els.imageViewerMeta.textContent = part.originalUrl && part.originalUrl !== part.url
    ? "模型输出图片 · 当前图为稳定预览版"
    : "模型输出图片";
  els.imageViewerImg.src = part.url;
  els.imageViewerImg.alt = part.alt || "assistant image output";
  els.downloadImageBtn.href = part.url;
  els.downloadImageBtn.download = buildImageDownloadName({
    title: part.alt,
    mimeType: part.mimeType,
    url: part.url,
    fallback: "generated-image",
  });

  if (part.alt) {
    els.imageViewerCaption.textContent = part.alt;
    els.imageViewerCaption.classList.remove("hidden");
  }

  if (part.originalUrl && part.originalUrl !== part.url) {
    els.openOriginalImageBtn.href = part.originalUrl;
    els.openOriginalImageBtn.classList.remove("hidden");
  }

  openImageViewer();
}

async function openSourcePreview(source) {
  const doc = await getDocumentFromStateOrApi(source.document_id);
  resetPreviewDrawer();
  if (els.previewEyebrow) {
    els.previewEyebrow.textContent = "引用文件";
  }
  els.previewTitle.textContent = doc.source_name || source.source_name || "文件预览";

  const pageLabel = source.locator_label
    || (Number.isInteger(source.page_no) ? `Page ${Number(source.page_no)}` : null);
  const metaLines = [
    `状态：${formatStatusLabel(doc.status)}`,
    `类型：${formatContentTypeLabel(doc.content_type)}${doc.mime_type ? ` (${doc.mime_type})` : ""}`,
    Number.isFinite(Number(doc.size_bytes)) ? `大小：${formatBytes(doc.size_bytes)}` : null,
    Number.isInteger(doc.page_count) ? `页数：${doc.page_count}` : null,
    pageLabel || (source.chunk_index === null || source.chunk_index === undefined ? null : `定位：第 ${Number(source.chunk_index) + 1} 段`),
    doc.error_message ? `错误：${doc.error_message}` : null,
  ].filter(Boolean);

  for (const line of metaLines) {
    appendPreviewMetaLine(line);
  }

  if (source.snippet) {
    els.previewSnippet.classList.remove("hidden");
    els.previewSnippet.textContent = source.snippet;
  } else {
    els.previewSnippet.classList.add("hidden");
    els.previewSnippet.textContent = "";
  }

  const filePreviewUrl = buildDocumentFileUrl(doc.document_id);
  const normalizedType = String(doc.content_type || "").toLowerCase();
  if (["png", "jpg", "jpeg", "webp"].includes(normalizedType)) {
    appendPreviewMetaLink({
      href: filePreviewUrl,
      text: "打开原图",
    });
    setPreviewDownloadButton({
      url: filePreviewUrl,
      fileName: doc.source_name || `image.${normalizedType}`,
      label: "下载图片",
    });

    const image = document.createElement("img");
    image.src = filePreviewUrl;
    image.alt = doc.source_name || "image preview";
    image.className = "preview-media-frame image";
    els.previewMedia.appendChild(image);
    els.previewMedia.classList.remove("hidden");

    const previewHint = [
      "以下文本仅在模型不支持视觉输入时，作为 OCR/文本回退内容使用：",
      doc.content || "(no extracted text)",
    ].join("\n\n");
    showPreviewContent(previewHint);
  } else if (normalizedType === "pdf") {
    appendPreviewMetaLink({
      href: filePreviewUrl,
      text: "打开原文件预览",
    });

    const frame = document.createElement("iframe");
    frame.src = filePreviewUrl;
    frame.className = "preview-media-frame pdf";
    frame.title = doc.source_name || "pdf preview";
    els.previewMedia.appendChild(frame);
    els.previewMedia.classList.remove("hidden");

    const previewHint = [
      "以下文本主要用于检索与不支持原生 PDF/视觉输入时的回退：",
      doc.content || "",
    ].join("\n");
    showPreviewContent(previewHint);
  } else if (["docx", "xlsx"].includes(normalizedType)) {
    appendPreviewMetaLink({
      href: filePreviewUrl,
      text: "打开原文件",
    });
    setPreviewDownloadButton({
      url: filePreviewUrl,
      fileName: doc.source_name || `attachment.${normalizedType}`,
      label: "下载原文件",
    });

    const previewHint = [
      normalizedType === "docx"
        ? "以下内容为从 Word 提取的文本，供检索与问答使用："
        : "以下内容为从 Excel 提取的结构化文本，供检索与问答使用：",
      doc.content || "",
    ].join("\n\n");
    showPreviewContent(previewHint);
  } else {
    showPreviewContent(doc.content || "");
  }
  openPreviewDrawer();
}

function closePreviewDrawer() {
  resetPreviewDrawer();
  els.previewDrawer.classList.add("hidden");
  els.previewDrawer.setAttribute("aria-hidden", "true");
}

function buildDocumentFileUrl(documentId) {
  const query = new URLSearchParams({ team_id: state.teamId });
  if (state.conversationId) {
    query.set("conversation_id", state.conversationId);
  }
  return `${API_PREFIX}/documents/${encodeURIComponent(documentId)}/file?${query.toString()}`;
}

async function getDocumentFromStateOrApi(documentId) {
  const cached = state.documents.find((item) => item.document_id === documentId);
  if (cached) {
    return cached;
  }

  const query = new URLSearchParams({ team_id: state.teamId });
  if (state.conversationId) {
    query.set("conversation_id", state.conversationId);
  }
  return apiRequest(`/documents/${encodeURIComponent(documentId)}?${query.toString()}`);
}

function showImportComposer() {
  els.importComposer.classList.remove("hidden");
  if (!els.sourceName.value.trim()) {
    els.sourceName.focus();
  } else {
    els.fileContent.focus();
  }
}

function hideImportComposer() {
  els.importComposer.classList.add("hidden");
  els.sourceName.value = "";
  els.fileContent.value = "";
}

function closeAttachMenu() {
  els.attachMenu.classList.add("hidden");
}

async function ensureConversationReady() {
  if (state.conversationId) {
    setRequiresFreshConversation(false);
    return;
  }
  if (state.requiresFreshConversation) {
    await createAndSwitchConversation({
      silent: true,
      restoreLaunch: false,
      closeSurface: false,
      focusComposer: false,
    });
    return;
  }
  await loadConversations();
  await ensureActiveConversation();
}

function syncSendButtonState() {
  if (!els.sendBtn) {
    return;
  }
  els.sendBtn.disabled = state.sending;
  els.sendBtn.textContent = state.sending ? "生成中..." : "发送";
  els.sendBtn.classList.toggle("loading", state.sending);
  refreshWorkspaceUi();
}

async function maybeAutoTitleConversation(question) {
  const activeConversation = getActiveConversation();
  if (!activeConversation || !state.conversationId) {
    return;
  }

  const currentTitle = String(activeConversation.title || "").trim();
  if (currentTitle && currentTitle !== DEFAULT_CONVERSATION_TITLE) {
    return;
  }

  const normalizedTitle = truncate(String(question || "").replace(/\s+/g, " ").trim(), 22);
  if (!normalizedTitle || normalizedTitle === currentTitle) {
    return;
  }

  await apiRequest(`/conversations/${encodeURIComponent(state.conversationId)}`, {
    method: "PATCH",
    body: {
      team_id: state.teamId,
      user_id: state.userId,
      title: normalizedTitle,
    },
  });
}

async function handleSend() {
  const question = els.messageInput.value.trim();
  if (!question || state.sending) {
    return;
  }

  if (!ensureIdentity()) {
    openAuthModal();
      showToast("请先登录", true);
    return;
  }

  await ensureConversationReady();
  setWorkspaceStage(WORKSPACE_STAGE_CHAT);
  setActiveSurface(ACTIVE_SURFACE_NONE);
  state.sending = true;
  syncSendButtonState();

  appendMessage("user", question);
  const usingAgentMode = state.agentMode;
  const selectedAgentApp = usingAgentMode ? getSelectedAgentApp() : null;
  const usingDocumentScope = getReadyDocumentCount() > 0;
  const pendingLabel = usingAgentMode
    ? (selectedAgentApp ? `Agent 应用「${selectedAgentApp.name}」正在执行…` : "Agent 正在规划、检索并检查工具动作…")
    : usingDocumentScope
      ? (getExplicitSelectedDocumentIds().length
        ? "正在基于选中文件检索并生成回答…"
        : "正在结合本会话资料增强回答…")
      : "正在思考并生成回答…";
  appendPendingAssistantMessage(pendingLabel);
  els.messageInput.value = "";
  autoGrowTextarea();

  try {
    const payload = usingAgentMode
      ? {
        user_id: state.userId,
        team_id: state.teamId,
        conversation_id: state.conversationId,
        space_id: getActiveSpaceId(),
        task: question,
        top_k: 5,
        dry_run: false,
        confirm_dangerous_actions: false,
        embedding_model: state.selectedEmbedding || DEFAULT_EMBEDDING_ID,
      }
      : {
        user_id: state.userId,
        team_id: state.teamId,
        conversation_id: state.conversationId,
        question,
        top_k: 5,
        use_document_scope: usingDocumentScope,
        include_memory: false,
        include_library: false,
        embedding_model: state.selectedEmbedding || DEFAULT_EMBEDDING_ID,
      };

    if (state.selectedModel !== DEFAULT_MODEL_ID) {
      payload.model = state.selectedModel;
    }

    const selectedDocumentIds = getExplicitSelectedDocumentIds();
    if (usingDocumentScope && selectedDocumentIds.length) {
      payload.selected_document_ids = selectedDocumentIds;
    }

    if (usingAgentMode) {
      // Streaming agent path
      const runsPath = selectedAgentApp
        ? `/apps/${encodeURIComponent(selectedAgentApp.app_id)}/runs`
        : "/agent/runs";
      const createResponse = await apiRequest(runsPath, { method: "POST", body: payload });

      await consumeAgentStream(createResponse.stream_url, question);
      await Promise.all([
        loadHistory(),
        maybeAutoTitleConversation(question).catch(() => null),
      ]);
      await loadConversations();
    } else {
      const requestPath = "/chat/ask";
      await apiRequest(requestPath, {
        method: "POST",
        body: payload,
      });
      removePendingAssistantMessage();
      await Promise.all([
        loadHistory(),
        maybeAutoTitleConversation(question).catch(() => null),
      ]);
      await loadConversations();
    }
  } catch (error) {
    removePendingAssistantMessage();
    const message = String(error.message || "发送失败");
    const fallbackResult = await tryEchoFallback(question, message);
    if (fallbackResult) {
      await Promise.all([
        loadHistory(),
        maybeAutoTitleConversation(question).catch(() => null),
      ]);
      await loadConversations();
      showToast(fallbackResult.notice);
    } else {
      appendMessage("assistant", `请求失败：${message}`);
      showToast(message, true);
    }
  } finally {
    state.sending = false;
    syncSendButtonState();
  }
}

function getExplicitSelectedDocumentIds() {
  return state.selectedDocumentIds.filter((documentId) => {
    return state.documents.some((doc) => doc.document_id === documentId && normalizeStatus(doc.status) === "ready");
  });
}

function looksLikeImageGenerationPrompt(question) {
  const text = String(question || "").trim().toLowerCase();
  if (!text) {
    return false;
  }

  const keywords = [
    "generate image",
    "generate an image",
    "create image",
    "create an image",
    "draw ",
    "illustration",
    "poster",
    "logo",
    "diagram",
    "生成图片",
    "生成一张图",
    "生成图像",
    "画一张",
    "画个",
    "绘制",
    "海报",
    "插画",
    "配图",
    "图片",
    "图像",
    "示意图",
    "流程图",
  ];
  return keywords.some((keyword) => text.includes(keyword));
}

function shouldUseEchoFallback(errorMessage, question) {
  const text = String(errorMessage || "").toLowerCase();
  if (looksLikeImageGenerationPrompt(question)) {
    return false;
  }
  return (
    text.includes("no indexed chunks found")
    || text.includes("llm_api_key is required")
  );
}

async function tryEchoFallback(question, errorMessage) {
  if (!shouldUseEchoFallback(errorMessage, question)) {
    return null;
  }

  try {
    const echoResponse = await apiRequest("/chat/echo", {
      method: "POST",
      body: {
        user_id: state.userId,
        team_id: state.teamId,
        conversation_id: state.conversationId,
        message: question,
      },
    });

    return {
      answer: `离线回复：${echoResponse.answer || question}`,
      notice: "已自动切换离线回复（Echo）",
    };
  } catch {
    return null;
  }
}

async function consumeAgentStream(streamUrl, question) {
  return new Promise((resolve, reject) => {
    const fullUrl = `${window.location.origin}${API_PREFIX}${streamUrl}`;
    const source = new EventSource(fullUrl);

    let accumulatedText = "";
    let currentStatus = "";
    let agentRunId = "";
    let requiredConfirmations = [];

    const updatePendingLabel = (text) => {
      const pendingEl = document.querySelector(".message.assistant.pending .message-content .message-text");
      if (pendingEl) {
        pendingEl.textContent = text || "Agent is thinking...";
      }
    };

    source.addEventListener("llm.delta", (e) => {
      try {
        const event = JSON.parse(e.data);
        if (event.payload && event.payload.text) {
          accumulatedText += event.payload.text;
          updatePendingLabel(accumulatedText);
        }
      } catch {}
    });

    source.addEventListener("tool.proposed", (e) => {
      try {
        const event = JSON.parse(e.data);
        if (event.payload && event.payload.tool_name) {
          updatePendingLabel(`Agent is using tool: ${event.payload.tool_name}...`);
        }
      } catch {}
    });

    source.addEventListener("tool.started", (e) => {
      try {
        const event = JSON.parse(e.data);
        if (event.payload && event.payload.tool_name) {
          updatePendingLabel(`Executing: ${event.payload.tool_name}...`);
        }
      } catch {}
    });

    source.addEventListener("tool.result", (e) => {
      try {
        const event = JSON.parse(e.data);
        updatePendingLabel("Agent is analyzing tool results...");
      } catch {}
    });

    source.addEventListener("confirmation.required", (e) => {
      try {
        const event = JSON.parse(e.data);
        requiredConfirmations = [event.payload || {}];
        agentRunId = event.run_id || "";
        updatePendingLabel("Confirmation required. Please approve the tool execution.");
      } catch {}
    });

    source.addEventListener("run.completed", (e) => {
      source.close();
      try {
        const event = JSON.parse(e.data);
        const payload = event.payload || {};
        const finalAnswer = payload.answer || accumulatedText || "Agent has completed.";
        agentRunId = payload.run_id || agentRunId;
        const status = payload.status || "completed";
        const steps = Array.isArray(payload.steps) ? payload.steps : [];
        const sources = Array.isArray(payload.sources) ? payload.sources : [];

        removePendingAssistantMessage();
        appendMessage("assistant", finalAnswer, {
          createdAt: payload.completed_at || payload.created_at || new Date().toISOString(),
          sources: normalizeResponseSources({ sources }),
          agentRunId,
          agentStatus: status,
          agentSteps: steps,
          requiredConfirmations: requiredConfirmations,
        });
        resolve(payload);
      } catch {
        removePendingAssistantMessage();
        appendMessage("assistant", accumulatedText || "Agent has completed.");
        resolve({});
      }
    });

    source.addEventListener("run.failed", (e) => {
      source.close();
      try {
        const event = JSON.parse(e.data);
        const error = (event.payload && event.payload.error) || "Agent execution failed.";
        removePendingAssistantMessage();
        appendMessage("assistant", `Agent 执行失败：${error}`);
        showToast(error, true);
      } catch {
        removePendingAssistantMessage();
        appendMessage("assistant", "Agent 执行失败。");
      }
      reject(new Error((e.data && JSON.parse(e.data).payload && JSON.parse(e.data).payload.error) || "Agent execution failed"));
    });

    source.addEventListener("step.completed", (e) => {
      // Trace step completion silently
    });

    source.addEventListener("run.started", (e) => {
      try {
        const event = JSON.parse(e.data);
        agentRunId = event.run_id || "";
      } catch {}
    });

    source.onerror = (e) => {
      source.close();
      removePendingAssistantMessage();
      if (accumulatedText) {
        appendMessage("assistant", accumulatedText);
      } else {
        appendMessage("assistant", "Stream 连接中断，请重试。");
      }
      reject(new Error("SSE connection error"));
    };
  });
}

async function handleImportFromComposer() {
  const sourceName = els.sourceName.value.trim();
  const content = els.fileContent.value.trim();
  if (!sourceName || !content) {
    showToast("请填写文件名并粘贴内容", true);
    return;
  }

  await importDocumentWithContent({
    sourceName,
    content,
    contentType: inferTextContentType(sourceName),
  });
  hideImportComposer();
}

async function importDocumentWithContent({ sourceName, content, contentType }) {
  if (state.importing) {
    return;
  }
  if (!ensureIdentity()) {
    openAuthModal();
      showToast("请先登录", true);
    return;
  }

  await ensureConversationReady();
  state.importing = true;
  setButtonLoading(els.importBtn, true, "导入中...");
  refreshWorkspaceUi();

  try {
    const doc = await apiRequest("/documents/import", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        conversation_id: state.conversationId,
        source_name: sourceName,
        content_type: contentType,
        content,
        auto_index: true,
        embedding_model: state.selectedEmbedding || DEFAULT_EMBEDDING_ID,
      },
    });

    upsertDocumentState(doc);
    renderDocuments();
    await pollDocumentsUntilSettled([doc.document_id]);
    const latest = state.documents.find((item) => item.document_id === doc.document_id);
    if (latest && normalizeStatus(latest.status) === "failed") {
      showToast(`导入失败：${latest.error_message || latest.error_code || sourceName}`, true);
    } else {
      showToast(`导入完成：${sourceName}`);
    }
  } catch (error) {
    await loadDocuments();
    showToast(error.message, true);
  } finally {
    state.importing = false;
    setButtonLoading(els.importBtn, false, "导入");
    refreshWorkspaceUi();
  }
}

function upsertDocumentState(doc) {
  const existingIndex = state.documents.findIndex((item) => item.document_id === doc.document_id);
  if (existingIndex >= 0) {
    state.documents[existingIndex] = doc;
  } else {
    state.documents = [doc, ...state.documents];
  }
}

function setDocumentStatusLocal(documentId, status) {
  state.documents = state.documents.map((doc) => {
    if (doc.document_id === documentId) {
      return { ...doc, status };
    }
    return doc;
  });
  renderDocuments();
}

async function handleFileInputChange(event) {
  const files = event.target.files ? Array.from(event.target.files) : [];
  event.target.value = "";
  if (!files.length) {
    return;
  }

  for (const file of files) {
    try {
      await uploadDocumentFile(file);
    } catch (error) {
      showToast(error.message || "上传文件失败", true);
      break;
    }
  }
}

async function uploadDocumentFile(file) {
  if (state.importing) {
    showToast("正在上传中，请稍候…", true);
    return;
  }
  inferUploadContentType(file.name);
  if (!ensureIdentity()) {
    openAuthModal();
      showToast("请先登录", true);
    return;
  }

  await ensureConversationReady();
  state.importing = true;
  setButtonLoading(els.importBtn, true, "上传中...");
  refreshWorkspaceUi();

  try {
    const formData = new FormData();
    formData.append("team_id", state.teamId);
    formData.append("user_id", state.userId);
    formData.append("conversation_id", state.conversationId);
    formData.append("auto_index", "true");
    formData.append("embedding_model", state.selectedEmbedding || DEFAULT_EMBEDDING_ID);
    formData.append("file", file);

    const doc = await apiRequest("/documents/upload", {
      method: "POST",
      formData,
    });

    upsertDocumentState(doc);
    renderDocuments();

    await pollDocumentsUntilSettled([doc.document_id]);
    const latest = state.documents.find((item) => item.document_id === doc.document_id);
    if (latest && normalizeStatus(latest.status) === "failed") {
      showToast(`上传失败：${latest.error_message || latest.error_code || file.name}`, true);
    } else {
      showToast(`上传完成：${file.name}`);
    }
  } finally {
    state.importing = false;
    setButtonLoading(els.importBtn, false, "导入");
    refreshWorkspaceUi();
  }
}

function hasDraggedFiles(event) {
  return Boolean(
    event.dataTransfer
    && event.dataTransfer.types
    && Array.from(event.dataTransfer.types).includes("Files")
  );
}

function preventWindowFileDrop(event) {
  if (!hasDraggedFiles(event)) {
    return;
  }
  event.preventDefault();
}

function handleComposerDragEnter(event) {
  if (!hasDraggedFiles(event)) {
    return;
  }
  event.preventDefault();
  state.dragCounter += 1;
  setComposerDropActive(true);
}

function handleComposerDragOver(event) {
  if (!hasDraggedFiles(event)) {
    return;
  }
  event.preventDefault();
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = "copy";
  }
  setComposerDropActive(true);
}

function handleComposerDragLeave(event) {
  if (!hasDraggedFiles(event)) {
    return;
  }
  event.preventDefault();
  state.dragCounter = Math.max(0, state.dragCounter - 1);
  if (state.dragCounter === 0) {
    setComposerDropActive(false);
  }
}

async function handleComposerDrop(event) {
  if (!hasDraggedFiles(event)) {
    return;
  }
  event.preventDefault();
  state.dragCounter = 0;
  setComposerDropActive(false);

  const files = event.dataTransfer ? Array.from(event.dataTransfer.files || []) : [];
  if (!files.length) {
    return;
  }

  for (const file of files) {
    try {
      await uploadDocumentFile(file);
    } catch (error) {
      showToast(error.message || "上传文件失败", true);
      break;
    }
  }
}

function setComposerDropActive(active) {
  if (!els.composerZone) {
    return;
  }
  els.composerZone.classList.toggle("drop-active", Boolean(active));
}

async function pollDocumentsUntilSettled(documentIds) {
  if (!Array.isArray(documentIds) || !documentIds.length) {
    return;
  }

  const deduped = dedupeStrings(documentIds);
  const deadline = Date.now() + DOCUMENT_STATUS_POLL_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await sleep(DOCUMENT_STATUS_POLL_INTERVAL_MS);
    await loadDocuments();
    const targets = state.documents.filter((doc) => deduped.includes(doc.document_id));
    if (!targets.length) {
      return;
    }
    const allSettled = targets.every((doc) => {
      const normalized = normalizeStatus(doc.status);
      return normalized === "ready" || normalized === "failed";
    });
    if (allSettled) {
      return;
    }
  }
}

function createMessageShell(role, speakerName) {
  const shell = document.createElement("div");
  shell.className = "message-shell";

  const avatar = document.createElement("div");
  avatar.className = `message-avatar ${role}`;
  avatar.textContent = role === "user"
    ? (speakerName || "你").slice(0, 1)
    : "宝";

  const column = document.createElement("div");
  column.className = "message-column";

  if (role === "user") {
    shell.append(column, avatar);
  } else {
    shell.append(avatar, column);
  }

  return { shell, column };
}

function appendPendingAssistantMessage(label = "正在思考并生成回答…") {
  removePendingAssistantMessage();
  setHeroVisible(false);

  const row = document.createElement("div");
  row.className = "message-row assistant pending";

  const { shell, column } = createMessageShell("assistant", "CaiBao");

  const message = document.createElement("article");
  message.className = "message assistant pending";

  const head = document.createElement("div");
  head.className = "message-head";

  const left = document.createElement("span");
  left.textContent = "CaiBao";

  const right = document.createElement("span");
  right.textContent = "处理中";

  head.append(left, right);

  const body = document.createElement("div");
  body.className = "message-body pending-body";

  const indicator = document.createElement("div");
  indicator.className = "pending-indicator";
  for (let index = 0; index < 3; index += 1) {
    const dot = document.createElement("span");
    dot.className = "pending-dot";
    indicator.appendChild(dot);
  }

  const text = document.createElement("div");
  text.className = "pending-text";
  text.textContent = label;

  const skeleton = document.createElement("div");
  skeleton.className = "pending-skeleton";
  for (let index = 0; index < 2; index += 1) {
    const line = document.createElement("span");
    line.className = "pending-line";
    skeleton.appendChild(line);
  }

  body.append(indicator, text, skeleton);
  message.append(head, body);
  column.appendChild(message);
  row.appendChild(shell);

  pendingAssistantRow = row;
  els.messageList.appendChild(row);
  refreshWorkspaceUi();
  scrollToBottom();
}

function removePendingAssistantMessage() {
  if (pendingAssistantRow?.parentNode) {
    pendingAssistantRow.parentNode.removeChild(pendingAssistantRow);
  }
  pendingAssistantRow = null;
  refreshWorkspaceUi();
}

function appendMessage(role, content, options = {}) {
  const contentParts = role === "assistant" ? normalizeContentParts(options.contentParts) : [];
  if (!content && !contentParts.length) {
    return;
  }

  setHeroVisible(false);

  const row = document.createElement("div");
  row.className = `message-row ${role}`;
  if (options.messageId) {
    row.dataset.messageId = options.messageId;
  }

  const speakerName = role === "user" ? (state.displayName || "你") : "CaiBao";
  const { shell, column } = createMessageShell(role, speakerName);

  const message = document.createElement("article");
  message.className = `message ${role}`;

  const head = document.createElement("div");
  head.className = "message-head";

  const left = document.createElement("span");
  left.textContent = speakerName;

  const createdDate = options.createdAt ? new Date(options.createdAt) : new Date();
  const timeText = Number.isNaN(createdDate.valueOf()) ? formatClock(new Date()) : formatClock(createdDate);

  const right = document.createElement("span");
  if (role === "assistant") {
    const meta = [timeText];
    if (options.mode) {
      meta.push(options.mode);
    }
    if (options.model) {
      meta.push(options.model);
    }
    right.textContent = meta.join(" · ");
  } else {
    right.textContent = timeText;
  }

  head.append(left, right);

  const body = document.createElement("div");
  body.className = "message-body";
  if (role === "assistant" && contentParts.length) {
    body.classList.add("rich-content");
    renderAssistantContent(body, contentParts, content);
  } else {
    body.textContent = content;
  }

  message.append(head, body);
  if (role === "assistant" && Array.isArray(options.sources) && options.sources.length) {
    const sourceBox = document.createElement("div");
    sourceBox.className = "source-box";

    const title = document.createElement("div");
    title.className = "source-title";
    title.textContent = "引用来源";
    sourceBox.appendChild(title);

    const sourceList = document.createElement("div");
    sourceList.className = "source-list";
    for (const source of options.sources.slice(0, 4)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "source-pill";
      button.addEventListener("click", () => {
        openSourcePreview(source).catch((error) => showToast(error.message, true));
      });

      const sourceTitle = document.createElement("div");
      sourceTitle.className = "source-pill-title";
      const locator = source.locator_label
        || (Number.isInteger(source.page_no) ? `Page ${Number(source.page_no)}` : null)
        || (Number.isInteger(source.chunk_index) ? `第 ${Number(source.chunk_index) + 1} 段` : null);
      sourceTitle.textContent = locator
        ? `${source.source_name || "未命名文件"} · ${locator}`
        : (source.source_name || "未命名文件");

      const sourceSnippet = document.createElement("div");
      sourceSnippet.className = "source-pill-snippet";
      sourceSnippet.textContent = source.snippet || "点击查看文件预览";

      button.append(sourceTitle, sourceSnippet);
      sourceList.appendChild(button);
    }

    sourceBox.appendChild(sourceList);
    message.appendChild(sourceBox);
  }

  if (role === "assistant" && Array.isArray(options.agentSteps) && options.agentSteps.length) {
    renderAgentTrace(message, options);
  }

  const assistantCaptureContext = role === "assistant" && options.messageId && options.spaceId
    ? {
      messageId: options.messageId,
      spaceId: options.spaceId,
      channel: options.channel || "",
      requestText: options.requestText || "",
      answerText: extractCopyText(content, contentParts).trim(),
      sources: Array.isArray(options.sources) ? options.sources : [],
      createdAt: options.createdAt || null,
    }
    : null;

  const actionRail = document.createElement("div");
  actionRail.className = "message-action-rail";

  if (role === "user" && options.messageId) {
    if (options.editable) {
      actionRail.appendChild(createMessageActionButton("编辑", () => {
        editHistoryMessage(options.messageId, content, options.channel).catch((error) => {
          showToast(error.message, true);
        });
      }));
    }
    if (options.deletable) {
      actionRail.appendChild(createMessageActionButton("删除", () => {
        deleteHistoryMessage(options.messageId).catch((error) => {
          showToast(error.message, true);
        });
      }));
    }
  }

  if (role === "assistant") {
    if (assistantCaptureContext && assistantCaptureContext.channel !== "action") {
      const messageId = assistantCaptureContext.messageId;
      const favoriteRecord = getMessageCaptureRecord("favoritesByMessageId", messageId);

      actionRail.appendChild(createMessageActionButton(
        favoriteRecord ? "已收藏" : "收藏",
        () => {
          toggleAssistantFavorite(assistantCaptureContext).catch((error) => showToast(error.message, true));
        },
        {
          active: Boolean(favoriteRecord),
          disabled: isCaptureActionPending(messageId, "favorite"),
          title: favoriteRecord ? "这条回答已经收藏过了" : "收藏这条回答",
          pressed: Boolean(favoriteRecord),
        },
      ));
    }

    actionRail.appendChild(createMessageActionButton("复制", () => {
      copyMessageText(extractCopyText(content, contentParts)).catch((error) => showToast(error.message, true));
    }));

    if (options.regenerable && options.messageId && options.requestText) {
      actionRail.appendChild(createMessageActionButton("重新生成", () => {
        regenerateAssistantMessage(options.messageId, options.requestText, options.channel).catch((error) => {
          showToast(error.message, true);
        });
      }));
    }
  }

  if (actionRail.childElementCount > 0) {
    column.appendChild(actionRail);
  }

  column.prepend(message);
  row.appendChild(shell);
  els.messageList.appendChild(row);
  refreshWorkspaceUi();
  if (options.autoScroll !== false) {
    scrollToBottom();
  }
}

function renderAgentTrace(message, options = {}) {
  const traceBox = document.createElement("div");
  traceBox.className = "agent-trace-box";

  const head = document.createElement("div");
  head.className = "agent-trace-head";

  const title = document.createElement("span");
  title.textContent = "Agent 执行轨迹";

  const status = document.createElement("span");
  status.className = `agent-status ${normalizeStatus(options.agentStatus || "completed")}`;
  status.textContent = options.agentStatus || "completed";
  head.append(title, status);
  traceBox.appendChild(head);

  const list = document.createElement("ol");
  list.className = "agent-step-list";
  for (const step of options.agentSteps.slice(0, 8)) {
    const item = document.createElement("li");
    item.className = `agent-step ${normalizeStatus(step.status || "")}`;

    const stepTitle = document.createElement("span");
    stepTitle.className = "agent-step-title";
    stepTitle.textContent = `${step.step_type || "step"} · ${step.title || "Untitled"}`;

    const stepMeta = document.createElement("span");
    stepMeta.className = "agent-step-meta";
    stepMeta.textContent = [step.status, step.tool_name].filter(Boolean).join(" · ");

    item.append(stepTitle, stepMeta);
    list.appendChild(item);
  }
  traceBox.appendChild(list);

  const confirmations = Array.isArray(options.requiredConfirmations) ? options.requiredConfirmations : [];
  if (confirmations.length) {
    const confirmBox = document.createElement("div");
    confirmBox.className = "agent-confirm-box";
    const confirmText = document.createElement("span");
    confirmText.textContent = `需要确认：${confirmations.map((item) => item.tool_name).join(", ")}`;
    confirmBox.appendChild(confirmText);

    if (options.agentRunId) {
      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "message-icon-btn active";
      confirmBtn.textContent = "确认执行";
      confirmBtn.addEventListener("click", () => {
        confirmAgentRun(options.agentRunId).catch((error) => showToast(error.message, true));
      });
      confirmBox.appendChild(confirmBtn);
    }
    traceBox.appendChild(confirmBox);
  }

  message.appendChild(traceBox);
}

async function confirmAgentRun(runId) {
  const response = await apiRequest(`/agent/runs/${encodeURIComponent(runId)}/confirm`, {
    method: "POST",
    body: {},
  });
  appendMessage("assistant", response.answer || "Agent 已确认执行。", {
    createdAt: response.completed_at || response.created_at,
    sources: normalizeResponseSources(response),
    agentRunId: response.run_id || runId,
    agentStatus: response.status || "",
    agentSteps: Array.isArray(response.steps) ? response.steps : [],
    requiredConfirmations: Array.isArray(response.required_confirmations)
      ? response.required_confirmations
      : [],
  });
  showToast("Agent 已执行确认动作");
}

function renderAssistantContent(container, contentParts, fallbackText = "") {
  container.textContent = "";
  let rendered = false;

  for (const part of contentParts) {
    if (part.type === "text" && part.text) {
      const textBlock = document.createElement("div");
      textBlock.className = "message-part message-part-text";
      textBlock.textContent = part.text;
      container.appendChild(textBlock);
      rendered = true;
      continue;
    }

    if (part.type === "image" && part.url) {
      const frame = document.createElement("figure");
      frame.className = "message-part message-part-image";

      const link = document.createElement("a");
      link.className = "message-part-image-link";
      link.href = part.url;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        openAssistantImagePreview(part);
      });

      const image = document.createElement("img");
      image.className = "message-part-image-frame";
      image.src = part.url;
      image.alt = part.alt || "assistant image output";
      image.loading = "lazy";
      link.appendChild(image);
      frame.appendChild(link);

      if (part.alt) {
        const caption = document.createElement("figcaption");
        caption.className = "message-part-image-caption";
        caption.textContent = part.alt;
        frame.appendChild(caption);
      }

      container.appendChild(frame);
      rendered = true;
    }
  }

  if (!rendered && fallbackText) {
    container.textContent = fallbackText;
  }
}

function extractCopyText(content, contentParts = []) {
  const parts = Array.isArray(contentParts) ? contentParts : [];
  const text = parts
    .filter((part) => part.type === "text" && part.text)
    .map((part) => part.text)
    .join("\n\n")
    .trim();
  if (text) {
    return text;
  }
  return content || "";
}

function createMessageActionButton(label, onClick, options = {}) {
  const button = document.createElement("button");
  button.className = `message-icon-btn${options.active ? " active" : ""}`;
  button.type = "button";
  button.title = options.title || label;
  button.textContent = label;
  button.disabled = Boolean(options.disabled);
  if (typeof options.pressed === "boolean") {
    button.setAttribute("aria-pressed", String(options.pressed));
  }
  button.addEventListener("click", onClick);
  return button;
}

function normalizeResponseSources(payload) {
  if (Array.isArray(payload.sources) && payload.sources.length) {
    return payload.sources.map((item) => ({
      document_id: item.document_id,
      source_name: item.source_name || "",
      chunk_id: item.chunk_id || "",
      chunk_index: Number.isFinite(Number(item.chunk_index)) ? Number(item.chunk_index) : null,
      page_no: Number.isFinite(Number(item.page_no)) ? Number(item.page_no) : null,
      locator_label: item.locator_label || "",
      snippet: item.snippet || "",
      score: Number(item.score || 0),
    }));
  }

  if (Array.isArray(payload.hits) && payload.hits.length) {
    return payload.hits.map((item) => ({
      document_id: item.document_id,
      source_name: item.source_name || "",
      chunk_id: item.chunk_id || "",
      chunk_index: Number.isFinite(Number(item.chunk_index)) ? Number(item.chunk_index) : null,
      page_no: Number.isFinite(Number(item.page_no)) ? Number(item.page_no) : null,
      locator_label: item.locator_label || "",
      snippet: truncate(String(item.content || "").replace(/\s+/g, " ").trim(), 220),
      score: Number(item.score || 0),
    }));
  }

  return [];
}

function normalizeResponseContentParts(payload) {
  if (!payload || !Array.isArray(payload.content_parts) || !payload.content_parts.length) {
    return [];
  }
  return normalizeContentParts(payload.content_parts);
}

function normalizeContentParts(parts) {
  if (!Array.isArray(parts)) {
    return [];
  }

  return parts
    .map((item) => {
      const type = String(item?.type || "").trim().toLowerCase();
      if (type === "text") {
        const text = String(item?.text || "");
        if (!text) {
          return null;
        }
        return { type: "text", text };
      }

      if (type === "image") {
        const url = normalizeRenderableImageUrl(item?.url);
        if (!url) {
          return null;
        }
        return {
          type: "image",
          url,
          originalUrl: normalizeRenderableImageUrl(item?.original_url),
          alt: String(item?.alt || "").trim(),
          mimeType: String(item?.mime_type || "").trim(),
        };
      }

      return null;
    })
    .filter(Boolean);
}

function normalizeRenderableImageUrl(value) {
  const url = String(value || "").trim();
  if (!url) {
    return "";
  }
  if (url.startsWith("data:image/") || url.startsWith("https://") || url.startsWith("http://")) {
    return url;
  }
  return "";
}

function clearConversation() {
  removePendingAssistantMessage();
  els.messageList.innerHTML = "";
  setHeroVisible(true);
  refreshWorkspaceUi();
}

function setHeroVisible(visible) {
  setWorkspaceStage(visible ? WORKSPACE_STAGE_LAUNCH : WORKSPACE_STAGE_CHAT);
}

function applyScenarioCard(scene) {
  const templates = {
    direct: "我们先直接聊聊这个问题：请帮我梳理思路、给出关键判断，并列出下一步建议。",
    plan: "请基于这个目标帮我写方案，包含：背景、目标、里程碑、资源投入、风险与验收标准。",
    qa: "如果当前会话有资料，请优先结合资料回答；如果没有，请先基于通用知识给出结论，并标出还需要补充的信息。",
  };

  const prompt = templates[scene];
  if (!prompt) {
    return;
  }

  els.messageInput.value = prompt;
  autoGrowTextarea();
  els.messageInput.focus();
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    const container = document.querySelector(".conversation");
    if (!container) {
      return;
    }
    container.scrollTop = container.scrollHeight;
  });
}

function legacyOpenSettingsModal() {
  updateIdentityCard();
  refreshWorkspaceUi();
  if (els.settingsModal) {
    els.settingsModal.classList.remove("hidden");
  }
}

function legacyCloseSettingsModal() {
  if (els.settingsModal) {
    els.settingsModal.classList.add("hidden");
  }
}

function legacyOpenAuthModal() {
  legacyOpenSettingsModal();
}

function legacyCloseAuthModal() {
  legacyCloseSettingsModal();
}

function legacyOpenCustomModelModal() {
  els.customModelNameInput.value = "";
  els.customModelBaseUrlInput.value = "https://api.openai.com/v1";
  els.customModelApiKeyInput.value = "";
  els.customModelModal.classList.remove("hidden");
  els.customModelNameInput.focus();
}

function legacyCloseCustomModelModal() {
  els.customModelModal.classList.add("hidden");
  if (els.modelSelect) {
    els.modelSelect.value = state.selectedModel;
  }
}

function legacyOpenCustomEmbeddingModal() {
  els.customEmbeddingNameInput.value = "";
  els.customEmbeddingProviderInput.value = "openai";
  els.customEmbeddingBaseUrlInput.value = "https://api.openai.com/v1";
  els.customEmbeddingApiKeyInput.value = "";
  syncCustomEmbeddingFields();
  els.customEmbeddingModal.classList.remove("hidden");
  els.customEmbeddingNameInput.focus();
}

function legacyCloseCustomEmbeddingModal() {
  els.customEmbeddingModal.classList.add("hidden");
  if (els.embeddingSelect) {
    els.embeddingSelect.value = state.selectedEmbedding;
  }
}

function legacySyncCustomEmbeddingFields() {
  const isMock = els.customEmbeddingProviderInput.value === "mock";
  els.customEmbeddingBaseUrlField.classList.toggle("hidden", isMock);
  els.customEmbeddingApiKeyField.classList.toggle("hidden", isMock);
}

function ensureIdentity() {
  return Boolean(state.teamId && state.userId);
}

function getWorkspaceDisplayName() {
  return state.displayName || state.teamName || "未登录";
}

function getWorkspaceId() {
  return state.teamId || state.userId || "";
}

function getAiConfigurationSummary() {
  const usesCustomChatModel = ![DEFAULT_MODEL_ID, NONE_MODEL_ID].includes(state.selectedModel);
  const usesCustomEmbedding = ![DEFAULT_EMBEDDING_ID, MOCK_EMBEDDING_ID].includes(state.selectedEmbedding);
  return usesCustomChatModel || usesCustomEmbedding ? "已自定义" : "默认";
}

function getResponseModeLabel() {
  return getReadyDocumentCount() ? "资料增强" : "直接聊天";
}

function getResponseModeHint(readyCount) {
  if (!readyCount) {
    return "当前还没有 ready 文件，先直接聊天即可";
  }
  return state.selectedDocumentIds.length
    ? `已自动接入 ${state.selectedDocumentIds.length} 份选中资料`
    : `已自动接入 ${readyCount} 份可检索资料`;
}

function updateIdentityCard() {
  const name = getWorkspaceDisplayName();
  const workspaceId = getWorkspaceId();
  if (els.profileName) {
    els.profileName.textContent = name;
  }
  if (els.profileTeam) {
    els.profileTeam.textContent = workspaceId ? `账号 ID · ${workspaceId}` : "点击登录或注册";
  }
  if (els.avatarText) {
    els.avatarText.textContent = name.slice(0, 1) || "未";
  }
  if (els.loginUserIdInput) {
    els.loginUserIdInput.value = state.userId || "";
  }
  if (els.registerUserIdInput) {
    els.registerUserIdInput.value = state.userId || "";
  }
  if (els.registerDisplayNameInput) {
    els.registerDisplayNameInput.value = state.displayName || state.teamName || "";
  }
  if (els.settingsWorkspaceSummary) {
    els.settingsWorkspaceSummary.textContent = workspaceId
      ? `${name} · 账号 ID ${workspaceId}`
      : "尚未登录";
  }
  refreshWorkspaceUi();
}

function refreshWorkspaceUi() {
  const loggedIn = ensureIdentity();
  const activeConversation = getActiveConversation();
  const scope = getDocumentScopeSummary();
  const activeTitle = activeConversation?.title || "新会话";
  const readyCount = getReadyDocumentCount();
  const conversationRailActive = state.railMode === RAIL_MODE_EXPANDED;
  const filesRailActive = state.activeSurface === ACTIVE_SURFACE_FILES;

  if (els.historySectionTitle) {
    els.historySectionTitle.textContent = state.conversations.length
      ? `会话列表 · ${state.conversations.length}`
      : "会话列表";
  }
  if (els.documentSectionTitle) {
    els.documentSectionTitle.textContent = state.documents.length
      ? `本会话文件 · ${state.documents.length}`
      : "本会话文件";
  }
  if (els.railConversationsBtn) {
    els.railConversationsBtn.classList.toggle("active", conversationRailActive);
    els.railConversationsBtn.setAttribute("aria-pressed", String(conversationRailActive));
  }
  if (els.railFilesBtn) {
    els.railFilesBtn.classList.toggle("active", filesRailActive);
    els.railFilesBtn.setAttribute("aria-pressed", String(filesRailActive));
  }
  if (els.conversationCountValue) {
    els.conversationCountValue.textContent = String(state.conversations.length);
  }
  if (els.documentCountValue) {
    els.documentCountValue.textContent = String(state.documents.length);
  }
  if (els.readyDocumentCountValue) {
    els.readyDocumentCountValue.textContent = String(readyCount);
  }

  if (els.workspaceEyebrow) {
    els.workspaceEyebrow.textContent = loggedIn
      ? `当前账号 · ${getWorkspaceId()}`
      : "Personal AI Assistant";
  }
  if (els.workspaceDescription) {
    if (!loggedIn) {
      els.workspaceDescription.textContent = "进入工作区后即可直接聊天、写方案和整理想法；有资料时会自动接入回答。";
    } else {
      const desc = [activeTitle];
      if (state.history.length) {
        desc.push(`${state.history.length} 轮对话`);
      }
      desc.push(scope.label);
      els.workspaceDescription.textContent = desc.join(" · ");
    }
  }

  if (els.heroTitle) {
    if (!loggedIn) {
      els.heroTitle.textContent = "进入工作区后就能直接开聊，需要时再补充资料";
    } else if (!state.documents.length) {
      els.heroTitle.textContent = "先直接聊天，只有需要依据时再补充资料";
    } else if (!readyCount) {
      els.heroTitle.textContent = "现在就能继续聊，资料处理完成后会自动增强回答";
    } else {
      els.heroTitle.textContent = "直接聊天，或让资料为回答补充依据";
    }
  }
  if (els.heroSubtitle) {
    els.heroSubtitle.textContent = loggedIn
      ? scope.hint
      : "不用先上传文件。只有当你需要更稳妥的依据、引用和范围控制时，才打开资料增强。";
  }
  if (els.heroAccount) {
    els.heroAccount.textContent = getWorkspaceDisplayName();
  }
  if (els.heroAccountHint) {
    els.heroAccountHint.textContent = loggedIn
      ? `账号 ID · ${getWorkspaceId()}`
      : "点击左下角登录或注册";
  }
  if (els.heroSession) {
    els.heroSession.textContent = getResponseModeLabel();
  }
  if (els.heroSessionHint) {
    els.heroSessionHint.textContent = getResponseModeHint(readyCount);
  }
  if (els.heroScope) {
    els.heroScope.textContent = scope.label;
  }
  if (els.heroScopeHint) {
    els.heroScopeHint.textContent = scope.hint;
  }

  if (els.composerPresence) {
    els.composerPresence.textContent = state.sending
      ? (state.agentMode ? "Agent 正在执行任务" : "CaiBao 正在整理回答")
      : (loggedIn ? `账号 · ${getWorkspaceDisplayName()}` : "尚未登录");
  }
  if (els.composerScope) {
    els.composerScope.textContent = `资料范围 · ${scope.label}`;
  }
  if (els.composerSession) {
    els.composerSession.textContent = state.agentMode
      ? `Agent 模式 · ${formatModelDisplayName(state.selectedModel)}`
      : `回答模型 · ${formatModelDisplayName(state.selectedModel)}`;
  }
  if (els.composerHint) {
    if (state.importing) {
      els.composerHint.textContent = "正在处理新资料，完成后会自动加入本会话检索范围。";
    } else if (!readyCount) {
      els.composerHint.textContent = "支持直接聊天、拖拽上传、粘贴文本和双击附件预览。";
    } else {
      els.composerHint.textContent = "回答会附带来源卡片，双击附件可快速预览原文。";
    }
  }
  if (els.shell) {
    els.shell.classList.toggle("has-history", Boolean(state.history.length || pendingAssistantRow));
  }
  if (els.settingsWorkspaceSummary) {
    els.settingsWorkspaceSummary.textContent = loggedIn
      ? `${getWorkspaceDisplayName()} · 账号 ID ${getWorkspaceId()}`
      : "尚未登录";
  }

  syncWorkspaceStage();
  syncActiveSurface();
  syncWorkspaceView();
}

function initModelOptions() {
  const configuredModels = state.modelConfigs.map((item) => item.model_name);
  const allModels = dedupeStrings([DEFAULT_MODEL_ID, NONE_MODEL_ID, ...configuredModels]);
  if (!els.modelSelect && !els.quickModelSelect && !els.quickModelSelectInline) {
    return;
  }
  syncOptionSelect(els.modelSelect, allModels, formatModelOptionLabel);
  syncOptionSelect(els.quickModelSelect, allModels, formatModelOptionLabel);
  syncOptionSelect(els.quickModelSelectInline, allModels, formatModelOptionLabel);
  state.selectedModel = allModels.includes(state.selectedModel) ? state.selectedModel : DEFAULT_MODEL_ID;
  syncSelectedModelControls();
  persistSelectedModel();
  refreshWorkspaceUi();
}

function initEmbeddingOptions() {
  if (!els.embeddingSelect) {
    return;
  }
  const configuredModels = state.embeddingConfigs.map((item) => item.model_name);
  const allModels = dedupeStrings([DEFAULT_EMBEDDING_ID, MOCK_EMBEDDING_ID, ...configuredModels]);
  els.embeddingSelect.innerHTML = "";

  for (const model of allModels) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = formatEmbeddingOptionLabel(model);
    els.embeddingSelect.appendChild(option);
  }

  state.selectedEmbedding = allModels.includes(state.selectedEmbedding) ? state.selectedEmbedding : DEFAULT_EMBEDDING_ID;
  els.embeddingSelect.value = state.selectedEmbedding;
  persistSelectedEmbedding();
  refreshWorkspaceUi();
}

function syncOptionSelect(selectEl, values, formatLabel) {
  if (!selectEl) {
    return;
  }
  selectEl.innerHTML = "";
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = formatLabel(value);
    selectEl.appendChild(option);
  }
}

function syncSelectedModelControls() {
  if (els.modelSelect) {
    els.modelSelect.value = state.selectedModel;
  }
  if (els.quickModelSelect) {
    els.quickModelSelect.value = state.selectedModel;
  }
  if (els.quickModelSelectInline) {
    els.quickModelSelectInline.value = state.selectedModel;
  }
}

async function handleModelChange(sourceSelect = els.quickModelSelectInline || els.modelSelect || els.quickModelSelect) {
  if (!sourceSelect) {
    return;
  }
  const selected = sourceSelect.value;
  state.selectedModel = selected;
  persistSelectedModel();
  syncSelectedModelControls();
  if (selected === DEFAULT_MODEL_ID) {
    showToast("当前使用系统默认回答模型");
  } else if (selected === NONE_MODEL_ID) {
    showToast("当前使用 none（仅输出 mock 回复）");
  } else {
    showToast(`默认回答模型已切换为 ${selected}`);
  }
  refreshWorkspaceUi();
}

async function handleEmbeddingChange() {
  if (!els.embeddingSelect) {
    return;
  }
  const selected = els.embeddingSelect.value;
  state.selectedEmbedding = selected;
  persistSelectedEmbedding();
  if (selected === DEFAULT_EMBEDDING_ID) {
    showToast("当前使用系统默认向量模型");
  } else if (selected === MOCK_EMBEDDING_ID) {
    showToast("当前使用 mock 向量配置");
  } else {
    showToast(`默认向量模型已切换为 ${selected}`);
  }
  refreshWorkspaceUi();
}

async function saveCustomModelConfig() {
  const normalizedModelName = els.customModelNameInput?.value.trim() || "";
  const baseUrl = els.customModelBaseUrlInput?.value.trim() || "";
  const apiKey = els.customModelApiKeyInput?.value.trim() || "";

  if (!normalizedModelName || normalizedModelName.toLowerCase() === DEFAULT_MODEL_ID || normalizedModelName.toLowerCase() === NONE_MODEL_ID) {
    showToast("模型名称无效", true);
    return;
  }
  if (!baseUrl) {
    showToast("请填写 API Base URL", true);
    return;
  }
  if (!apiKey) {
    showToast("请填写 API Key", true);
    return;
  }

  setButtonLoading(els.saveCustomModelBtn, true, "保存中...");
  try {
    await apiRequest("/llm/models", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        model_name: normalizedModelName,
        base_url: baseUrl,
        api_key: apiKey,
      },
    });

    state.selectedModel = normalizedModelName;
    persistSelectedModel();
    await loadModelConfigs();
    closeCustomModelModal();
    showToast(`已添加并切换到模型 ${normalizedModelName}`);
  } finally {
    setButtonLoading(els.saveCustomModelBtn, false, "保存模型");
  }
}

async function saveCustomEmbeddingConfig() {
  const normalizedModelName = els.customEmbeddingNameInput?.value.trim() || "";
  const provider = els.customEmbeddingProviderInput?.value.trim().toLowerCase() || "";
  const baseUrl = els.customEmbeddingBaseUrlInput?.value.trim() || "";
  const apiKey = els.customEmbeddingApiKeyInput?.value.trim() || "";

  if (!normalizedModelName || normalizedModelName.toLowerCase() === DEFAULT_EMBEDDING_ID) {
    showToast("向量模型名称无效", true);
    return;
  }
  if (!provider) {
    showToast("请选择 provider", true);
    return;
  }
  if (provider !== "mock" && !baseUrl) {
    showToast("请填写 Embedding API Base URL", true);
    return;
  }
  if (provider !== "mock" && !apiKey) {
    showToast("请填写 Embedding API Key", true);
    return;
  }

  setButtonLoading(els.saveCustomEmbeddingBtn, true, "保存中...");
  try {
    await apiRequest("/embedding/models", {
      method: "POST",
      body: {
        team_id: state.teamId,
        user_id: state.userId,
        model_name: normalizedModelName,
        provider,
        base_url: provider === "mock" ? null : baseUrl,
        api_key: provider === "mock" ? null : apiKey,
      },
    });

    state.selectedEmbedding = normalizedModelName;
    persistSelectedEmbedding();
    await loadEmbeddingConfigs();
    closeCustomEmbeddingModal();
    showToast(`已添加并切换到向量模型 ${normalizedModelName}`);
  } finally {
    setButtonLoading(els.saveCustomEmbeddingBtn, false, "保存向量模型");
  }
}

async function legacyHandleSaveAuthV2() {
  return handleLoginSubmit();
}

async function handleLoginSubmit() {
  const userId = normalizeAuthUserId(els.loginUserIdInput?.value);
  const password = String(els.loginPasswordInput?.value || "");

  if (!userId || !password) {
    setAuthError("请输入账号 ID 和密码。");
    return;
  }
  if (password.length < 8) {
    setAuthError("密码至少需要 8 个字符。");
    return;
  }

  setAuthError("");
  setButtonLoading(els.loginAuthBtn, true, "登录中...");
  try {
    const session = await apiRequest("/auth/login", {
      method: "POST",
      body: {
        user_id: userId,
        password,
      },
      retryOn401: false,
    });
    await finalizeAuthSuccess(session, `已登录：${session.display_name || session.user_id}`);
  } catch (error) {
    setAuthError(error.message);
  } finally {
    setButtonLoading(els.loginAuthBtn, false, "登录");
  }
}

async function handleRegisterSubmit() {
  const userId = normalizeAuthUserId(els.registerUserIdInput?.value);
  const displayName = String(els.registerDisplayNameInput?.value || "").trim().slice(0, 128);
  const password = String(els.registerPasswordInput?.value || "");
  const confirmPassword = String(els.registerConfirmPasswordInput?.value || "");

  if (!userId || !displayName || !password || !confirmPassword) {
    setAuthError("请完整填写注册信息。");
    return;
  }
  if (password.length < 8) {
    setAuthError("密码至少需要 8 个字符。");
    return;
  }
  if (confirmPassword.length < 8) {
    setAuthError("确认密码至少需要 8 个字符。");
    return;
  }
  if (password !== confirmPassword) {
    setAuthError("两次输入的密码不一致。");
    return;
  }

  setAuthError("");
  setButtonLoading(els.registerAuthBtn, true, "注册中...");
  try {
    const session = await apiRequest("/auth/register", {
      method: "POST",
      body: {
        user_id: userId,
        display_name: displayName,
        password,
        confirm_password: confirmPassword,
      },
      retryOn401: false,
    });
    await finalizeAuthSuccess(session, `已创建账号：${session.display_name || session.user_id}`);
  } catch (error) {
    setAuthError(error.message);
  } finally {
    setButtonLoading(els.registerAuthBtn, false, "注册并进入");
  }
}

async function handleLogout() {
  if (!ensureIdentity()) {
    handleSignedOutState({ openAuthDialog: true });
    return;
  }

  setButtonLoading(els.logoutBtn, true, "退出中...");
  try {
    await apiRequest("/auth/logout", {
      method: "POST",
      retryOn401: false,
    });
  } catch (error) {
    if (error?.status && error.status !== 401) {
      throw error;
    }
  } finally {
    setButtonLoading(els.logoutBtn, false, "退出登录");
  }

  handleSignedOutState({ openAuthDialog: true });
  showToast("已退出登录");
}

function openAuthModal(mode = AUTH_MODE_LOGIN) {
  updateIdentityCard();
  setAuthMode(mode);
  if (els.loginPasswordInput) {
    els.loginPasswordInput.value = "";
  }
  if (els.registerPasswordInput) {
    els.registerPasswordInput.value = "";
  }
  if (els.registerConfirmPasswordInput) {
    els.registerConfirmPasswordInput.value = "";
  }
  if (els.authModal) {
    els.authModal.classList.remove("hidden");
  }
  const nextFocus = state.authMode === AUTH_MODE_REGISTER
    ? els.registerUserIdInput
    : (els.loginUserIdInput || els.loginPasswordInput);
  if (nextFocus instanceof HTMLElement) {
    window.setTimeout(() => nextFocus.focus(), 0);
  }
}

function closeAuthModal() {
  setAuthError("");
  if (els.authModal) {
    els.authModal.classList.add("hidden");
  }
}

function openSettingsModal() {
  if (!ensureIdentity()) {
    openAuthModal(AUTH_MODE_LOGIN);
    return;
  }
  setActiveSurface(ACTIVE_SURFACE_SETTINGS);
  updateIdentityCard();
}

function closeSettingsModal() {
  setActiveSurface(ACTIVE_SURFACE_NONE);
  refreshWorkspaceUi();
}

function openCustomModelModal() {
  if (!ensureIdentity()) {
    openAuthModal();
      showToast("请先登录", true);
    return;
  }
  if (els.customModelNameInput) {
    els.customModelNameInput.value = "";
  }
  if (els.customModelBaseUrlInput) {
    els.customModelBaseUrlInput.value = "https://api.openai.com/v1";
  }
  if (els.customModelApiKeyInput) {
    els.customModelApiKeyInput.value = "";
  }
  if (els.customModelModal) {
    els.customModelModal.classList.remove("hidden");
  }
}

function closeCustomModelModal() {
  if (els.customModelModal) {
    els.customModelModal.classList.add("hidden");
  }
}

function openCustomEmbeddingModal() {
  if (!ensureIdentity()) {
    openAuthModal();
      showToast("请先登录", true);
    return;
  }
  if (els.customEmbeddingNameInput) {
    els.customEmbeddingNameInput.value = "";
  }
  if (els.customEmbeddingProviderInput) {
    els.customEmbeddingProviderInput.value = "openai";
  }
  if (els.customEmbeddingBaseUrlInput) {
    els.customEmbeddingBaseUrlInput.value = "https://api.openai.com/v1";
  }
  if (els.customEmbeddingApiKeyInput) {
    els.customEmbeddingApiKeyInput.value = "";
  }
  syncCustomEmbeddingFields();
  if (els.customEmbeddingModal) {
    els.customEmbeddingModal.classList.remove("hidden");
  }
}

function closeCustomEmbeddingModal() {
  if (els.customEmbeddingModal) {
    els.customEmbeddingModal.classList.add("hidden");
  }
}

function syncCustomEmbeddingFields() {
  const provider = els.customEmbeddingProviderInput?.value || "openai";
  const isMock = provider === "mock";
  if (els.customEmbeddingBaseUrlField) {
    els.customEmbeddingBaseUrlField.classList.toggle("hidden", isMock);
  }
  if (els.customEmbeddingApiKeyField) {
    els.customEmbeddingApiKeyField.classList.toggle("hidden", isMock);
  }
}

function autoGrowTextarea() {
  els.messageInput.style.height = "auto";
  els.messageInput.style.height = `${Math.min(els.messageInput.scrollHeight, 180)}px`;
}

function appendEmpty(listEl, text) {
  const li = document.createElement("li");
  li.className = "empty-item";
  li.textContent = text;
  listEl.appendChild(li);
}

function dedupeStrings(values) {
  const result = [];
  for (const item of values) {
    const value = String(item || "").trim();
    if (value && !result.includes(value)) {
      result.push(value);
    }
  }
  return result;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatClock(dateObj) {
  const hour = String(dateObj.getHours()).padStart(2, "0");
  const minute = String(dateObj.getMinutes()).padStart(2, "0");
  return `${hour}:${minute}`;
}

function formatTime(iso) {
  const dt = new Date(iso);
  if (Number.isNaN(dt.valueOf())) {
    return "--";
  }
  return `${dt.getMonth() + 1}-${dt.getDate()} ${formatClock(dt)}`;
}

function formatBytes(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size <= 0) {
    return "0 B";
  }

  const units = ["B", "KB", "MB", "GB"];
  let current = size;
  let unitIndex = 0;
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024;
    unitIndex += 1;
  }
  const digits = current >= 100 || unitIndex === 0 ? 0 : 1;
  return `${current.toFixed(digits)} ${units[unitIndex]}`;
}

function formatContentTypeLabel(contentType) {
  const normalized = String(contentType || "").trim().toLowerCase();
  const labels = {
    txt: "TXT 文本",
    md: "Markdown",
    pdf: "PDF",
    docx: "Word",
    xlsx: "Excel",
    png: "PNG 图片",
    jpg: "JPG 图片",
    jpeg: "JPEG 图片",
    webp: "WebP 图片",
  };
  return labels[normalized] || (normalized ? normalized.toUpperCase() : "未知类型");
}

function truncate(text, length) {
  if (text.length <= length) {
    return text;
  }
  return `${text.slice(0, length - 1)}…`;
}

function sleep(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function normalizeStatus(status) {
  const normalized = String(status || "uploaded").trim().toLowerCase();
  if (["pending", "uploaded", "parsing", "chunking", "indexing", "ready", "failed", "deleted",
       "completed", "requires_confirmation", "partial_success", "running", "skipped"].includes(normalized)) {
    return normalized;
  }
  return "uploaded";
}

function formatStatusLabel(status) {
  switch (normalizeStatus(status)) {
    case "uploaded":
      return "已上传";
    case "parsing":
      return "解析中";
    case "chunking":
      return "切块中";
    case "indexing":
      return "索引中";
    case "ready":
      return "可用";
    case "failed":
      return "失败";
    case "deleted":
      return "已删除";
    case "pending":
      return "待处理";
    default:
      return "已上传";
  }
}

function inferTextContentType(sourceName) {
  const normalized = String(sourceName || "").trim().toLowerCase();
  const candidates = ["txt", "md"];
  for (const ext of candidates) {
    if (normalized.endsWith(`.${ext}`)) {
      return ext;
    }
  }
  if (!normalized) {
    return "md";
  }
  throw new Error("粘贴导入仅支持 .txt/.md 文件名。");
}

function inferUploadContentType(sourceName) {
  const normalized = String(sourceName || "").trim().toLowerCase();
  const candidates = ["txt", "md", "pdf", "docx", "xlsx", "png", "jpg", "jpeg", "webp"];
  for (const ext of candidates) {
    if (normalized.endsWith(`.${ext}`)) {
      return ext;
    }
  }
  if (!normalized) {
    return "md";
  }
  throw new Error("当前仅支持 .txt/.md/.pdf/.docx/.xlsx/.png/.jpg/.jpeg/.webp 文件。");
}

function extractResponseDetail(data, fallbackDetail) {
  if (data && typeof data === "object" && "detail" in data) {
    if (Array.isArray(data.detail)) {
      const validationMessage = formatValidationErrors(data.detail);
      if (validationMessage) {
        return validationMessage;
      }
    }
    return String(data.detail || fallbackDetail || "");
  }
  if (typeof data === "string" && data.trim()) {
    return data.trim();
  }
  return String(fallbackDetail || "");
}

function formatValidationErrors(details) {
  if (!Array.isArray(details)) {
    return "";
  }
  const messages = [];
  for (const item of details) {
    const formatted = formatValidationErrorItem(item);
    if (formatted && !messages.includes(formatted)) {
      messages.push(formatted);
    }
  }
  return messages.join("；");
}

function formatValidationErrorItem(item) {
  if (!item || typeof item !== "object") {
    return "";
  }

  const loc = Array.isArray(item.loc) ? item.loc : [];
  const fieldName = String(loc[loc.length - 1] || "").trim();
  const label = VALIDATION_FIELD_LABELS[fieldName] || fieldName;
  const errorType = String(item.type || "").trim();
  const msg = String(item.msg || "").trim();

  if (errorType === "string_too_short") {
    const minLength = item.ctx && typeof item.ctx === "object" ? item.ctx.min_length : "";
    return label && minLength ? `${label}至少需要 ${minLength} 个字符。` : msg;
  }

  if (errorType === "missing") {
    return label ? `${label}不能为空。` : msg;
  }

  if (errorType === "json_invalid") {
    return "请求格式不正确，请刷新页面后重试。";
  }

  return label && msg ? `${label}：${msg}` : msg;
}

function createRequestError(status, detail) {
  const error = new Error(String(detail || `请求失败（${status}）`));
  error.status = status;
  return error;
}

async function requestJson(path, options = {}) {
  const useFormData = options.formData instanceof FormData;
  const requestOptions = {
    method: options.method || "GET",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(!useFormData && options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
    body: useFormData ? options.formData : (options.body ? JSON.stringify(options.body) : undefined),
  };

  const response = await fetch(`${API_PREFIX}${path}`, requestOptions);
  const text = await response.text();
  let data;

  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  return { response, data };
}

function canRetryWithRefresh(path, options, status) {
  if (status !== 401 || options.retryOn401 === false) {
    return false;
  }
  return ![
    "/auth/login",
    "/auth/register",
    "/auth/refresh",
    "/auth/logout",
  ].some((prefix) => path.startsWith(prefix));
}

function shouldHandleUnauthorizedAsSignedOut(path) {
  if (!path.startsWith("/auth/")) {
    return true;
  }
  return path.startsWith("/auth/me") || path.startsWith("/auth/refresh");
}

async function refreshAuthSession() {
  if (refreshSessionPromise) {
    return refreshSessionPromise;
  }

  refreshSessionPromise = (async () => {
    const { response, data } = await requestJson("/auth/refresh", {
      method: "POST",
      retryOn401: false,
    });
    if (!response.ok) {
      throw createRequestError(response.status, extractResponseDetail(data, response.statusText));
    }
    applyAuthSession(data);
    updateIdentityCard();
    return data;
  })().finally(() => {
    refreshSessionPromise = null;
  });

  return refreshSessionPromise;
}

async function apiRequest(path, options = {}) {
  const { response, data } = await requestJson(path, options);
  if (response.ok) {
    return data;
  }

  if (canRetryWithRefresh(path, options, response.status)) {
    try {
      await refreshAuthSession();
    } catch (error) {
      handleSignedOutState({ openAuthDialog: true });
      throw createRequestError(401, error.message || "登录状态已失效，请重新登录。");
    }

    const retried = await requestJson(path, { ...options, retryOn401: false });
    if (retried.response.ok) {
      return retried.data;
    }
    if (retried.response.status === 401 && shouldHandleUnauthorizedAsSignedOut(path)) {
      handleSignedOutState({ openAuthDialog: true });
    }
    throw createRequestError(
      retried.response.status,
      extractResponseDetail(retried.data, retried.response.statusText),
    );
  }

  if (response.status === 401 && shouldHandleUnauthorizedAsSignedOut(path)) {
    handleSignedOutState({ openAuthDialog: true });
  }
  throw createRequestError(response.status, extractResponseDetail(data, response.statusText));
}

function setButtonLoading(button, loading, loadingText) {
  if (!button) {
    return;
  }

  if (loading) {
    button.dataset.originText = button.textContent;
    button.textContent = loadingText;
    button.disabled = true;
    return;
  }

  button.textContent = button.dataset.originText || button.textContent;
  button.disabled = false;
}

function showToast(message, isError = false) {
  clearTimeout(toastTimer);
  els.toast.textContent = message;
  els.toast.classList.remove("hidden", "error");
  if (isError) {
    els.toast.classList.add("error");
  }
  toastTimer = setTimeout(() => {
    els.toast.classList.add("hidden");
  }, 2600);
}
