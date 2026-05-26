import re


def _get_web_app_script(client) -> str:
    response = client.get("/web/app.js")
    assert response.status_code == 200
    return response.text


def _get_web_index(client) -> str:
    response = client.get("/")
    assert response.status_code == 200
    return response.text


def _get_web_styles(client) -> str:
    response = client.get("/web/styles.css")
    assert response.status_code == 200
    return response.text


def _get_web_admin_html(client) -> str:
    response = client.get("/web/admin.html")
    assert response.status_code == 200
    return response.text


def _get_web_theme_styles(client) -> str:
    response = client.get("/web/theme.css")
    assert response.status_code == 200
    return response.text


def _get_web_admin_styles(client) -> str:
    response = client.get("/web/admin.css")
    assert response.status_code == 200
    return response.text


def _get_web_admin_script(client) -> str:
    response = client.get("/web/admin.js")
    assert response.status_code == 200
    return response.text


def test_chat_and_admin_load_shared_theme_stylesheet(client) -> None:
    index_html = _get_web_index(client)
    admin_html = _get_web_admin_html(client)

    assert 'href="/web/theme.css' in index_html
    assert 'href="/web/theme.css' in admin_html


def test_theme_styles_define_tokens_soft_grid_and_shared_motion(client) -> None:
    css = _get_web_theme_styles(client)

    assert "--accent: #15695f;" in css
    assert ".soft-grid-field" in css
    assert ".surface-card" in css
    assert ".shared-pill" in css
    assert "@keyframes grid-drift" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_workspace_specific_styles_consume_shared_foundation_tokens(client) -> None:
    theme_css = _get_web_theme_styles(client)
    chat_css = _get_web_styles(client)
    admin_css = _get_web_admin_styles(client)

    assert "--muted:" in theme_css
    assert "--danger:" in theme_css

    for token in (
        "--bg:",
        "--ink:",
        "--ink-soft:",
        "--line:",
        "--accent:",
        "--accent-strong:",
        "--panel:",
        "--panel-strong:",
        "--muted:",
        "--danger:",
        "--radius-xl:",
        "--radius-lg:",
        "--radius-md:",
        "--radius-sm:",
        "--shadow-soft:",
        "--shadow-strong:",
    ):
        assert token not in chat_css

    for token in (
        "--bg:",
        "--ink:",
        "--line:",
        "--accent:",
        "--panel:",
        "--muted:",
        "--danger:",
    ):
        assert token not in admin_css


def test_workspace_specific_styles_do_not_redeclare_reduced_motion_foundation(client) -> None:
    chat_css = _get_web_styles(client)
    admin_css = _get_web_admin_styles(client)

    assert "@media (prefers-reduced-motion: reduce)" not in chat_css
    assert "@media (prefers-reduced-motion: reduce)" not in admin_css


def test_chat_message_card_exposes_favorite_as_only_capture_action(client) -> None:
    script = _get_web_app_script(client)
    capture_guard = 'if (assistantCaptureContext && assistantCaptureContext.channel !== "action") {'
    copy_action = "copyMessageText(extractCopyText(content, contentParts))"

    capture_start = script.find(capture_guard)
    assert capture_start != -1

    capture_end = script.find(copy_action, capture_start)
    assert capture_end != -1

    capture_body = script[capture_start:capture_end]
    assert "toggleAssistantFavorite(assistantCaptureContext)" in capture_body
    assert "promoteFavoriteToMemory" not in capture_body
    assert "promoteFavoriteToConclusion" not in capture_body
    assert "publishFavoriteToLibrary" not in capture_body


def test_chat_message_capture_loader_only_prefetches_favorites(client) -> None:
    script = _get_web_app_script(client)
    loader_block = re.search(
        r"async function loadMessageCaptures\(\) \{(?P<body>.*?)\n\}",
        script,
        re.DOTALL,
    )
    assert loader_block is not None

    loader_body = loader_block.group("body")
    assert "/favorites/answers?" in loader_body
    assert "/memory/cards?" not in loader_body
    assert "/conclusions?" not in loader_body
    assert "/library/documents?" not in loader_body


def test_homepage_uses_expandable_workspace_rail(client) -> None:
    html = _get_web_index(client)

    assert 'id="railToggleBtn"' in html
    assert 'data-rail-mode="collapsed"' in html
    assert 'class="workspace-top-nav"' not in html
    assert 'class="workspace-switch-strip"' in html
    assert 'class="workspace-brand-badge"' in html
    assert 'class="workspace-brand-wordmark"' in html
    assert 'class="workspace-brand-name"' in html
    assert 'class="workspace-brand-mark"' not in html
    assert '<span class="workspace-brand-name">CAIBAO</span>' in html
    assert 'class="workspace-nav-tabs" role="tablist"' in html
    assert 'id="chatWorkspaceBtn"' in html
    assert 'id="favoritesWorkspaceBtn"' in html
    assert 'class="workspace-top-link" href="/web/admin.html"' not in html
    assert 'class="settings-section settings-section-advanced"' in html
    assert 'class="modal-link settings-cta-link" href="/web/admin.html"' in html
    assert 'class="rail-workspace-switch"' not in html
    assert 'class="workspace-rail-label"' in html
    assert 'class="rail-summary surface-card"' not in html


def test_frontend_tracks_persisted_rail_mode(client) -> None:
    script = _get_web_app_script(client)

    assert 'const RAIL_MODE_COLLAPSED = "collapsed";' in script
    assert 'const RAIL_MODE_EXPANDED = "expanded";' in script
    assert "railMode: RAIL_MODE_COLLAPSED" in script
    assert 'railMode: "caibao.railMode"' in script
    assert "function setRailMode(mode) {" in script
    assert "function toggleRailMode() {" in script
    assert 'els.railToggleBtn = document.getElementById("railToggleBtn");' in script
    assert 'els.railToggleBtn.addEventListener("click", toggleRailMode);' in script
    assert 'els.workspaceRail?.setAttribute("aria-expanded", String(expanded));' in script
    assert 'els.workspaceRail?.setAttribute("aria-hidden", String(!expanded));' in script
    assert 'els.workspaceRail.inert = !expanded;' in script


def test_styles_define_collapsed_and_expanded_rail_states(client) -> None:
    css = _get_web_styles(client)

    assert '.shell[data-rail-mode="collapsed"]' in css
    assert '.shell[data-rail-mode="expanded"]' in css
    assert ".rail-toggle-btn" in css
    assert ".workspace-rail-label" in css
    assert ".workspace-top-nav" not in css
    assert ".workspace-switch-strip" in css
    assert ".workspace-brand-badge" in css
    assert ".workspace-brand-wordmark" in css
    assert ".workspace-brand-name" in css
    assert ".workspace-brand-mark" not in css
    assert '"Iowan Old Style", "Palatino Linotype", "Book Antiqua", Baskerville, "Times New Roman", serif' in css
    assert ".workspace-nav-tabs" in css
    assert ".workspace-top-switch-btn" in css
    assert ".workspace-top-link" in css
    assert ".workspace-refresh-btn" in css
    assert ".workspace-refresh-icon" in css
    assert '.shell[data-rail-mode="collapsed"] .workspace-switch-btn' in css
    assert '.shell[data-rail-mode="collapsed"] .workspace-link' in css
    assert "content: attr(data-short);" in css


def test_homepage_uses_soft_grid_pattern_field_for_launch_identity(client) -> None:
    html = _get_web_index(client)

    assert 'id="workspacePatternField"' in html
    assert 'class="soft-grid-field workspace-pattern-field"' in html
    assert 'class="soft-grid-plane"' in html
    assert 'id="heroPanel" class="hero-panel launch-panel"' in html


def test_styles_define_signature_motion_keyframes(client) -> None:
    css = _get_web_styles(client)

    assert "@keyframes hero-collapse" in css
    assert "@keyframes dock-settle" in css
    assert "@keyframes surface-recall" in css
    assert ".workspace-pattern-field" in css
    assert ".shell.workspace-stage-chat .workspace-pattern-field" in css


def test_frontend_syncs_workspace_stage_into_shell_and_pattern_visibility(client) -> None:
    script = _get_web_app_script(client)

    assert "function syncWorkspaceStage() {" in script
    assert 'els.shell?.classList.toggle("workspace-stage-chat", isChatStage);' in script
    assert 'els.shell?.classList.toggle("workspace-stage-launch", !isChatStage);' in script
    assert 'els.workspacePatternField = document.getElementById("workspacePatternField");' in script


def test_homepage_uses_dual_pane_favorites_workspace(client) -> None:
    html = _get_web_index(client)

    assert 'id="favoritesPanel"' in html
    assert 'id="favoritesListPane"' in html
    assert 'id="favoriteDetailPane"' in html
    assert 'id="favoriteEmptyState"' in html


def test_styles_define_favorites_list_and_detail_panes(client) -> None:
    css = _get_web_styles(client)

    assert ".favorites-layout" in css
    assert ".favorites-list-pane" in css
    assert ".favorites-detail-pane" in css
    assert ".favorite-empty-state" in css


def test_frontend_tracks_selected_favorite_and_renders_detail_pane(client) -> None:
    script = _get_web_app_script(client)

    assert "selectedFavoriteId:" in script
    assert "function setSelectedFavoriteId(favoriteId) {" in script
    assert "function renderFavoriteWorkspace() {" in script
    assert "function renderFavoriteDetailPane() {" in script


def test_homepage_exposes_local_surface_status_regions(client) -> None:
    html = _get_web_index(client)
    script = _get_web_app_script(client)
    css = _get_web_styles(client)

    assert 'id="conversationDrawerStatus"' in html
    assert 'id="fileDrawerStatus"' in html
    assert 'id="settingsStatus"' in html
    assert "function setSurfaceStatus(targetEl, message, isError = false) {" in script
    assert "function clearSurfaceStatus(targetEl) {" in script
    assert ".surface-status" in css


def test_homepage_embeds_history_list_inside_workspace_rail(client) -> None:
    html = _get_web_index(client)
    css = _get_web_styles(client)

    assert 'id="workspaceRail"' in html
    assert 'class="workspace-rail-history"' in html
    assert 'class="workspace-rail-history-head"' in html
    assert 'id="historyList"' in html
    assert 'id="conversationDrawer"' not in html
    assert 'id="fileDrawer" class="surface-drawer file-drawer hidden"' in html
    assert ".workspace-rail-history" in css
    assert ".rail-history-list" in css
    assert ".composer-context-action" in css


def test_workspace_rail_history_items_keep_stable_single_line_layout(client) -> None:
    css = _get_web_styles(client)

    assert ".workspace-rail-history .history-row {" in css
    assert "align-items: center;" in css
    assert ".workspace-rail-history .history-title-wrap {" in css
    assert "flex: 1;" in css
    assert ".workspace-rail-history .doc-card-action {" in css
    assert "display: block;" in css
    assert "white-space: nowrap;" in css
    assert "text-overflow: ellipsis;" in css
    assert ".workspace-rail-history .history-item {" in css
    assert "min-height: 44px;" in css


def test_workspace_stage_does_not_resize_workspace_rail(client) -> None:
    css = _get_web_styles(client)

    assert ".workspace-rail {" in css
    assert "padding: 18px 12px;" in css
    assert ".shell.workspace-stage-launch .workspace-rail {" not in css
    assert ".shell.workspace-stage-chat .workspace-rail {" not in css


def test_workspace_rail_hides_by_collapsing_shell_column_without_column_animation(client) -> None:
    css = _get_web_styles(client)

    assert ".shell {" in css
    assert '.shell[data-rail-mode="collapsed"] {' in css
    assert "grid-template-columns: 0 minmax(0, 1fr);" in css
    assert '.shell[data-rail-mode="expanded"] {' in css
    assert "grid-template-columns: 230px minmax(0, 1fr);" in css
    assert "transition: grid-template-columns 0.26s ease;" not in css
    assert '.shell[data-rail-mode="collapsed"] .workspace-rail {' in css
    assert "opacity: 0;" in css
    assert "pointer-events: none;" in css
    assert "position: relative;" in css


def test_workspace_rail_uses_simple_horizontal_motion_without_extra_main_area_animation(client) -> None:
    css = _get_web_styles(client)
    script = _get_web_app_script(client)

    rail_start = css.find(".workspace-rail {")
    rail_end = css.find(".workspace-rail-history {", rail_start)
    assert rail_start != -1
    assert rail_end != -1
    rail_block = css[rail_start:rail_end]
    assert "border-radius: 0;" in rail_block
    assert "border-radius: 28px;" not in rail_block

    assert "@keyframes rail-main-open" not in css
    assert "@keyframes rail-main-close" not in css
    assert ".shell.rail-motion" not in css
    assert "RAIL_MOTION_DURATION_MS" not in script
    assert "queueRailMotion(" not in script


def test_frontend_keeps_only_file_and_settings_as_overlay_surfaces(client) -> None:
    script = _get_web_app_script(client)

    assert "state.activeSurface = surface || ACTIVE_SURFACE_NONE;" in script
    assert "const useOverlayConversationPanel = isCompactWorkspaceLayout();" not in script
    assert "const showConversationSurface" not in script
    assert 'els.conversationDrawer?.classList.toggle("hidden", !showConversationSurface);' not in script
    assert 'els.fileDrawer?.classList.toggle("hidden", current !== ACTIVE_SURFACE_FILES);' in script
    assert 'els.settingsModal?.classList.toggle("hidden", current !== ACTIVE_SURFACE_SETTINGS);' in script
    assert "closeSettingsModal();" in script


def test_frontend_binds_rail_history_and_settings_controls(client) -> None:
    script = _get_web_app_script(client)

    assert 'els.railToggleBtn = document.getElementById("railToggleBtn");' in script
    assert 'els.railNewChatBtn = document.getElementById("railNewChatBtn");' in script
    assert 'els.profileSettingsBtn = document.getElementById("profileSettingsBtn");' in script
    assert 'els.historyList = document.getElementById("historyList");' in script
    assert 'els.conversationDrawer = document.getElementById("conversationDrawer");' not in script
    assert 'els.railSettingsBtn = document.getElementById("railSettingsBtn");' not in script
    assert 'els.drawerNewChatBtn = document.getElementById("drawerNewChatBtn");' not in script
    assert 'els.newSessionBtn = document.getElementById("newSessionBtn");' not in script
    assert 'els.composerContextRow = document.getElementById("composerContextRow");' in script

    bind_start = script.find("function bindEvents() {")
    bind_end = script.find("function handleGlobalDocumentClick(event)", bind_start)
    assert bind_start != -1
    assert bind_end != -1
    bind_body = script[bind_start:bind_end]
    assert 'els.railToggleBtn.addEventListener("click", toggleRailMode);' in bind_body
    assert 'els.railNewChatBtn.addEventListener("click"' in bind_body
    assert 'els.drawerNewChatBtn.addEventListener("click"' not in bind_body
    assert 'els.profileSettingsBtn.addEventListener("click", openSettingsModal);' in bind_body
    assert 'els.railSettingsBtn.addEventListener("click", openSettingsModal);' not in bind_body
    assert "createAndSwitchConversation()" in bind_body


def test_frontend_clicking_outside_file_drawer_closes_surface(client) -> None:
    script = _get_web_app_script(client)

    click_start = script.find("function handleGlobalDocumentClick(event) {")
    click_end = script.find("function hydrateState()", click_start)
    assert click_start != -1
    assert click_end != -1

    click_body = script[click_start:click_end]
    assert "state.activeSurface === ACTIVE_SURFACE_FILES" in click_body
    assert 'els.fileDrawer?.contains(target)' in click_body
    assert 'target instanceof Element' in click_body
    assert 'target.closest("#composerFilesBtn, #railFilesBtn")' in click_body
    assert "setActiveSurface(ACTIVE_SURFACE_NONE);" in click_body
    assert "refreshWorkspaceUi();" in click_body


def test_homepage_uses_launch_panel_and_dock_context_row(client) -> None:
    html = _get_web_index(client)

    assert 'id="heroPanel" class="hero-panel launch-panel"' in html
    context_start = html.find('<div id="composerContextRow" class="composer-context-row">')
    assert context_start != -1
    context_end = html.find('<div id="attachmentStrip" class="attachment-strip"></div>', context_start)
    assert context_end != -1
    context_body = html[context_start:context_end]
    assert 'id="composerPresence"' in context_body
    assert 'id="composerScope"' in context_body
    assert 'id="composerSession"' in context_body
    assert 'class="composer-context-row hidden"' not in html
    assert 'class="composer-status-row"' not in html
    assert 'class="chat-mode-row"' not in html
    assert '<section id="heroPanel" class="hero-panel launch-panel">' in html
    assert '<div class="launch-panel">' not in html


def test_frontend_renders_composer_context_row_and_refresh_helper(client) -> None:
    script = _get_web_app_script(client)

    render_start = script.find("function renderComposerContextRow() {")
    refresh_start = script.find("function refreshComposerChrome() {")
    assert render_start != -1
    assert refresh_start != -1

    refresh_end = script.find("function refreshWorkspaceUi()", refresh_start)
    assert refresh_end != -1

    render_body = script[render_start:refresh_start]
    assert "state.chatMode" in render_body
    assert "state.documents.length" in render_body
    assert "state.selectedDocumentIds.length" in render_body
    assert "getReadyDocumentCount()" in render_body
    assert 'filesAction.id = "composerFilesBtn";' in render_body
    assert 'filesActionLabel.textContent = "文件";' in render_body
    assert 'filesActionCount.className = "composer-context-action-count";' in render_body
    assert "ACTIVE_SURFACE_FILES" in render_body

    refresh_body = script[refresh_start:refresh_end]
    assert "renderComposerContextRow();" in refresh_body
    assert "renderAttachmentStrip();" in refresh_body

    auto_mode_start = script.find("function syncAutoChatMode() {")
    auto_mode_end = script.find("function setWorkspaceStage(stage) {", auto_mode_start)
    assert auto_mode_start != -1
    assert auto_mode_end != -1

    auto_mode_body = script[auto_mode_start:auto_mode_end]
    assert "getReadyDocumentCount()" in auto_mode_body
    assert "state.chatMode = readyCount ? CHAT_MODE_DOCS : CHAT_MODE_CHAT;" in auto_mode_body
    assert "state.selectedDocumentIds = [];" in auto_mode_body

    load_docs_start = script.find("async function loadDocuments() {")
    load_docs_end = script.find("function renderDocuments() {", load_docs_start)
    assert load_docs_start != -1
    assert load_docs_end != -1

    load_docs_body = script[load_docs_start:load_docs_end]
    assert "syncAutoChatMode();" in load_docs_body


def test_homepage_keeps_workspace_switch_visible(client) -> None:
    html = _get_web_index(client)

    assert 'class="workspace-intro-bar"' in html
    assert 'class="workspace-top-nav"' not in html
    assert 'class="workspace-switch-strip"' in html
    assert ">聊天<" in html
    assert ">Agent<" in html
    assert ">收藏夹<" in html
    assert ">Admin<" not in html
    assert ">前往开发者管理台<" in html
    assert 'id="chatWorkspaceBtn"' in html
    assert 'id="agentModeBtn"' in html
    assert 'id="favoritesWorkspaceBtn"' in html
    assert 'class="workspace-top-switch-btn active"' in html
    assert 'id="refreshAllBtn" class="workspace-refresh-btn"' in html
    assert ">Refresh data<" not in html
    assert 'id="workspaceEyebrow"' not in html
    assert 'id="workspaceDescription"' not in html
    assert "Focused workspace" not in html


def test_auth_modal_uses_login_and_register_forms(client) -> None:
    html = _get_web_index(client)

    assert 'id="authLoginTab"' in html
    assert 'id="authRegisterTab"' in html
    assert 'id="loginUserIdInput"' in html
    assert 'id="loginPasswordInput"' in html
    assert 'id="registerUserIdInput"' in html
    assert 'id="registerDisplayNameInput"' in html
    assert 'id="registerPasswordInput"' in html
    assert 'id="registerConfirmPasswordInput"' in html
    assert 'id="accountIdInput"' not in html
    assert 'id="accountNameInput"' not in html
    assert 'id="saveAuthBtn"' not in html


def test_settings_modal_exposes_account_controls(client) -> None:
    html = _get_web_index(client)

    assert 'id="switchAccountBtn"' in html
    assert 'id="logoutBtn"' in html
    assert 'id="switchWorkspaceBtn"' not in html


def test_model_configuration_uses_settings_modals_instead_of_prompt_flows(client) -> None:
    script = _get_web_app_script(client)

    assert "openSettingsModal" in script
    assert "openCustomModelModal" in script
    assert "openCustomEmbeddingModal" in script
    assert 'window.prompt("Input API Base URL"' not in script
    assert 'window.prompt("Input API Key")' not in script
    assert 'window.prompt("Input Embedding API Base URL"' not in script
    assert 'window.prompt("Input Embedding API Key")' not in script


def test_frontend_bootstrap_uses_auth_session_routes(client) -> None:
    script = _get_web_app_script(client)

    assert "bootstrapAuthSession" in script
    assert "/auth/me" in script
    assert "/auth/login" in script
    assert "/auth/register" in script
    assert "/auth/logout" in script
    assert "/auth/refresh" in script
    assert 'credentials: "same-origin"' in script
    assert "retryOn401" in script


def test_frontend_formats_fastapi_validation_errors_for_auth_forms(client) -> None:
    script = _get_web_app_script(client)

    assert "Array.isArray(data.detail)" in script
    assert "VALIDATION_FIELD_LABELS" in script
    assert "string_too_short" in script
    assert "min_length" in script


def test_auth_forms_validate_password_rules_before_submit(client) -> None:
    script = _get_web_app_script(client)

    assert "password.length < 8" in script
    assert "confirmPassword.length < 8" in script
    assert "password !== confirmPassword" in script


def test_frontend_bootstrap_no_longer_depends_on_team_user_upserts(client) -> None:
    script = _get_web_app_script(client)

    assert "/teams/${encodeURIComponent" not in script
    assert "/users/${encodeURIComponent" not in script


def test_favorite_workflow_supports_toggle_and_secondary_actions(client) -> None:
    script = _get_web_app_script(client)

    assert "/favorites/answers/" in script
    assert 'method: "DELETE"' in script
    assert "loadFavoriteWorkspaceAssets" in script
    assert 'getFavoriteWorkspaceAsset("memoriesByMessageId"' in script
    assert 'getFavoriteWorkspaceAsset("conclusionsByMessageId"' in script
    assert 'getFavoriteWorkspaceAsset("libraryDocsByMessageId"' in script
    assert "/promote-to-memory" in script
    assert "/promote-to-conclusion" in script
    assert "/conclusions/" in script
    assert "/archive" in script
    assert "/memory/cards/" in script
    assert "/documents/import" in script
    assert "function removeFavoriteMemory(" in script
    assert "function archiveFavoriteConclusion(" in script
    assert "function removeFavoriteLibraryDocument(" in script


def test_frontend_agent_mode_calls_agent_run_and_renders_trace(client) -> None:
    script = _get_web_app_script(client)
    css = _get_web_styles(client)

    assert "agentMode:" in script
    assert "els.agentModeBtn" in script
    assert ('"/agent/run"' in script or '"/agent/runs"' in script)
    assert "confirmAgentRun" in script
    assert "/agent/runs/${encodeURIComponent(runId)}/confirm" in script
    assert "renderAgentTrace" in script
    assert "Agent 执行轨迹" in script
    assert ".agent-trace-box" in css


def test_admin_uses_enterprise_command_bar_and_panel_grid(client) -> None:
    html = _get_web_admin_html(client)

    assert 'class="admin-shell admin-enterprise-shell"' in html
    assert 'id="adminCommandBar"' in html
    assert 'class="admin-panel-grid"' in html
    assert 'class="admin-table-panel"' in html


def test_admin_styles_define_command_bar_and_shared_panels(client) -> None:
    css = _get_web_admin_styles(client)

    assert ".admin-enterprise-shell" in css
    assert ".admin-command-bar" in css
    assert ".admin-panel-grid" in css
    assert ".admin-table-panel" in css


def test_admin_script_supports_escape_to_close_document_modal(client) -> None:
    script = _get_web_admin_script(client)

    assert 'document.addEventListener("keydown", (event) => {' in script
    assert 'if (event.key === "Escape") {' in script
    assert "closeDocModal();" in script


def test_frontend_workspace_stage_rail_toggles_use_active_surface_state(client) -> None:
    script = _get_web_app_script(client)

    conv_start = script.find("if (els.railConversationsBtn) {")
    conv_end = script.find("if (els.railFilesBtn) {", conv_start)
    assert conv_start != -1
    assert conv_end != -1

    conv_body = script[conv_start:conv_end]
    assert "if (state.railMode === RAIL_MODE_COLLAPSED) {" in conv_body
    assert "setRailMode(RAIL_MODE_EXPANDED);" in conv_body
    assert 'els.historyList?.querySelector(".history-item")?.focus();' in conv_body
    assert "state.activeSurface === ACTIVE_SURFACE_CONVERSATIONS" not in conv_body
    assert "els.drawerNewChatBtn?.focus();" not in conv_body

    file_start = conv_end
    file_end = script.find("els.newSessionBtn.addEventListener", file_start)
    if file_end == -1:
        file_end = script.find("if (els.modelSelect) {", file_start)
    assert file_end != -1

    file_body = script[file_start:file_end]
    assert "state.activeSurface === ACTIVE_SURFACE_FILES" in file_body
    assert "? ACTIVE_SURFACE_NONE" in file_body
    assert ": ACTIVE_SURFACE_FILES" in file_body


def test_frontend_workspace_stage_auth_success_resets_to_launch_with_no_surface(client) -> None:
    script = _get_web_app_script(client)

    auth_start = script.find("async function finalizeAuthSuccess(session, toastMessage) {")
    auth_end = script.find("async function bootstrapAuthSession()", auth_start)
    assert auth_start != -1
    assert auth_end != -1

    auth_body = script[auth_start:auth_end]
    assert "setWorkspaceStage(WORKSPACE_STAGE_LAUNCH);" in auth_body
    assert "setActiveSurface(ACTIVE_SURFACE_NONE);" in auth_body
    assert "setRequiresFreshConversation(true);" in auth_body
    assert "loadConversations()" in auth_body
    assert "els.messageInput.focus();" in auth_body

    send_start = script.find("async function handleSend() {")
    send_end = script.find("function getExplicitSelectedDocumentIds()", send_start)
    assert send_start != -1
    assert send_end != -1

    send_body = script[send_start:send_end]
    assert "setWorkspaceStage(WORKSPACE_STAGE_CHAT);" in send_body
    assert "setActiveSurface(ACTIVE_SURFACE_NONE);" in send_body


def test_frontend_new_chat_returns_to_launch_and_focuses_composer(client) -> None:
    script = _get_web_app_script(client)

    create_start = script.find("async function createAndSwitchConversation({")
    create_end = script.find("async function switchConversation(conversationId) {", create_start)
    assert create_start != -1
    assert create_end != -1

    create_body = script[create_start:create_end]
    assert "restoreLaunch = true" in create_body
    assert "closeSurface = true" in create_body
    assert "focusComposer = true" in create_body
    assert "if (restoreLaunch) {" in create_body
    assert "setWorkspaceStage(WORKSPACE_STAGE_LAUNCH);" in create_body
    assert "if (closeSurface) {" in create_body
    assert "setActiveSurface(ACTIVE_SURFACE_NONE);" in create_body
    assert "if (focusComposer && els.messageInput) {" in create_body
    assert "els.messageInput.focus();" in create_body


def test_frontend_requires_a_fresh_conversation_after_auth_success(client) -> None:
    script = _get_web_app_script(client)

    assert "requiresFreshConversation:" in script
    assert "function setRequiresFreshConversation(required)" in script

    create_start = script.find("async function createAndSwitchConversation({")
    create_end = script.find("async function switchConversation(conversationId) {", create_start)
    assert create_start != -1
    assert create_end != -1

    create_body = script[create_start:create_end]
    assert "setRequiresFreshConversation(false);" in create_body
    assert "await loadConversations();" in create_body

    switch_start = create_end
    switch_end = script.find("async function renameConversation(conversation)", switch_start)
    assert switch_end != -1

    switch_body = script[switch_start:switch_end]
    assert "setRequiresFreshConversation(false);" in switch_body

    ready_start = script.find("async function ensureConversationReady() {")
    ready_end = script.find("function syncSendButtonState()", ready_start)
    assert ready_start != -1
    assert ready_end != -1

    ready_body = script[ready_start:ready_end]
    assert "if (state.requiresFreshConversation) {" in ready_body
    assert "await createAndSwitchConversation({" in ready_body
    assert "silent: true," in ready_body
    assert "restoreLaunch: false," in ready_body
    assert "closeSurface: false," in ready_body
    assert "focusComposer: false," in ready_body
    assert "await ensureActiveConversation();" in ready_body


def test_frontend_persists_pending_fresh_conversation_across_reload(client) -> None:
    script = _get_web_app_script(client)

    assert 'pendingFreshConversationPrefix: "caibao.pendingFreshConversation"' in script
    assert "function freshConversationStorageKey()" in script
    assert "function loadFreshConversationRequirement()" in script
    assert "function persistFreshConversationRequirement()" in script

    apply_start = script.find("function applyAuthSession(session, { resetConversation = false } = {}) {")
    apply_end = script.find("function resetAuthenticatedWorkspace()", apply_start)
    assert apply_start != -1
    assert apply_end != -1

    apply_body = script[apply_start:apply_end]
    assert "state.requiresFreshConversation = loadFreshConversationRequirement();" in apply_body

    load_all_start = script.find("async function loadAllData() {")
    load_all_end = script.find("async function loadModelConfigs()", load_all_start)
    assert load_all_start != -1
    assert load_all_end != -1

    load_all_body = script[load_all_start:load_all_end]
    assert "if (state.requiresFreshConversation) {" in load_all_body
    assert "setWorkspaceStage(WORKSPACE_STAGE_LAUNCH);" in load_all_body
    assert "setActiveSurface(ACTIVE_SURFACE_NONE);" in load_all_body
    assert "clearConversation();" in load_all_body
    assert "await ensureActiveConversation();" in load_all_body

    signed_out_start = script.find("function handleSignedOutState({ openAuthDialog = false } = {}) {")
    signed_out_end = script.find("async function finalizeAuthSuccess(session, toastMessage) {", signed_out_start)
    assert signed_out_start != -1
    assert signed_out_end != -1

    signed_out_body = script[signed_out_start:signed_out_end]
    assert "setRequiresFreshConversation(false);" in signed_out_body


def test_homepage_keeps_preview_auth_and_settings_surfaces_while_using_recall_shell(client) -> None:
    html = _get_web_index(client)
    script = _get_web_app_script(client)

    assert 'id="previewDrawer"' in html
    assert 'id="imageViewer"' in html
    assert 'id="authModal"' in html
    assert 'id="settingsModal"' in html
    assert "openDocumentPreview(doc)" in script
    assert "openSettingsModal()" in script
    assert "openAuthModal(AUTH_MODE_LOGIN)" in script
    assert "syncAutoChatMode()" in script
    assert "doc.error_message || doc.error_code" in script


def test_frontend_exposes_agent_app_builder(client) -> None:
    html = _get_web_index(client)
    script = _get_web_app_script(client)

    assert 'id="agentAppsWorkspaceBtn"' in html
    assert 'id="agentAppsPanel"' in html
    assert "WORKSPACE_VIEW_AGENT_APPS" in script
    assert "loadAgentApps" in script
    assert 'apiRequest("/apps"' in script
    assert "/invoke" in script


def test_frontend_ignores_archived_conclusions_in_favorites_workspace(client) -> None:
    script = _get_web_app_script(client)

    load_assets_start = script.find("async function loadFavoriteWorkspaceAssets() {")
    load_assets_end = script.find("function renderFavoriteWorkspace() {", load_assets_start)
    assert load_assets_start != -1
    assert load_assets_end != -1

    load_assets_body = script[load_assets_start:load_assets_end]
    assert 'normalizeStatus(item?.status) === "archived"' in load_assets_body


def test_frontend_supports_escape_to_close_recall_surfaces(client) -> None:
    script = _get_web_app_script(client)

    assert 'if (event.key === "Escape") {' in script
    assert "setActiveSurface(ACTIVE_SURFACE_NONE)" in script
