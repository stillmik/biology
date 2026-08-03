const user = JSON.parse(localStorage.getItem("biology_user") || "null");
if (!user) window.location.href = "/register.html";

const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const messages = document.querySelector("#messages");
const status = document.querySelector("#status");
const fileInput = document.querySelector("#file-input");
const generateFileToggle = document.querySelector("#generate-file-toggle");
const attachmentStatus = document.querySelector("#attachment-status");
const conversationList = document.querySelector("#conversations");
const documentLibrary = document.querySelector("#document-library");
const activeDocuments = document.querySelector("#active-documents");
const sendButton = document.querySelector("#send-button");
let activeConversation = null;
let generateFile = false;
const unresolvedAnswerJobsByConversation = new Map();
const pendingConversationRequests = new Set();
let activeDocumentRecords = [];
document.querySelector("#username").textContent = user ? `@${user.username}` : "";

function isPdfFile(file) {
  return Boolean(file?.name.toLowerCase().endsWith(".pdf"));
}

function turnOffFileGeneration() {
  generateFile = false;
  generateFileToggle.classList.remove("is-active");
  generateFileToggle.setAttribute("aria-pressed", "false");
}

function updateFileGenerationAvailability() {
  const hasGroundedDocuments = activeDocumentRecords.length > 0 || isPdfFile(fileInput.files[0]);
  generateFileToggle.disabled = hasGroundedDocuments;

  if (hasGroundedDocuments) {
    turnOffFileGeneration();
    generateFileToggle.title = "Response files are unavailable for document-grounded answers";
    return;
  }

  generateFileToggle.title = generateFile ? "Generate a response file: on" : "Generate a PDF response file";
}

function updateComposerAvailability() {
  const activeConversationId = activeConversation?.id;
  const activeConversationHasPendingAnswer = unresolvedAnswerJobsByConversation.has(activeConversationId);
  const activeConversationHasPendingRequest = pendingConversationRequests.has(activeConversationId);
  setComposerBusy(activeConversationHasPendingAnswer || activeConversationHasPendingRequest);
}

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  attachmentStatus.textContent = file ? `Attached: ${file.name}` : "";
  updateFileGenerationAvailability();
});
generateFileToggle.addEventListener("click", () => {
  if (generateFileToggle.disabled) return;
  generateFile = !generateFile;
  generateFileToggle.classList.toggle("is-active", generateFile);
  generateFileToggle.setAttribute("aria-pressed", String(generateFile));
  updateFileGenerationAvailability();
});

function addMessage(text, role) {
  const group = document.createElement("div");
  group.className = `message-group ${role}`;
  const element = document.createElement("div");
  element.className = `message ${role}`;
  element.textContent = text;
  group.appendChild(element);
  messages.appendChild(group);
  messages.scrollTop = messages.scrollHeight;
  return element;
}

function addGeneratedFileLink(group, generatedFile) {
  if (!generatedFile?.id || group.querySelector(".generated-file-link")) return;
  const link = document.createElement("a");
  link.className = "generated-file-link";
  link.href = `/api/files/${encodeURIComponent(generatedFile.id)}?user_id=${encodeURIComponent(user.id)}`;
  link.textContent = `📄 ${generatedFile.filename}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  group.appendChild(link);
}

function getUserDisplayContent(content) {
  const pdfMarker = "\n\n[Attached PDF: ";
  const pdfMarkerIndex = content.indexOf(pdfMarker);
  if (pdfMarkerIndex >= 0) {
    const pdfMarkerEnd = content.indexOf(" | document:", pdfMarkerIndex);
    const pdfFilename = pdfMarkerEnd < 0 ? "PDF document" : content.slice(pdfMarkerIndex + pdfMarker.length, pdfMarkerEnd);
    return `${content.slice(0, pdfMarkerIndex)}\n\n📎 ${pdfFilename}`;
  }
  const attachmentMarker = "\n\n[Attached file: ";
  const markerIndex = content.indexOf(attachmentMarker);
  if (markerIndex < 0) return content;
  const attachmentEnd = content.indexOf("]", markerIndex);
  const filename = attachmentEnd < 0 ? "attachment" : content.slice(markerIndex + attachmentMarker.length, attachmentEnd);
  return `${content.slice(0, markerIndex)}\n\n📎 ${filename}`;
}

function escapeHtml(text) {
  return text.replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", "\"": "&quot;" })[character]);
}

function renderAssistantText(element, text) {
  const lines = text.split("\n");
  const output = [];
  let index = 0;
  let listItems = [];
  const flushList = () => {
    if (!listItems.length) return;
    output.push(`<ul>${listItems.join("")}</ul>`);
    listItems = [];
  };
  const inline = (value) => escapeHtml(value).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/)[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  while (index < lines.length) {
    if (lines[index].trim().startsWith("|") && index + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(lines[index + 1])) {
      const headers = lines[index].split("|").slice(1, -1).map((cell) => `<th>${inline(cell.trim())}</th>`).join("");
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        rows.push(`<tr>${lines[index].split("|").slice(1, -1).map((cell) => `<td>${inline(cell.trim())}</td>`).join("")}</tr>`);
        index += 1;
      }
      output.push(`<table><thead><tr>${headers}</tr></thead><tbody>${rows.join("")}</tbody></table>`);
      continue;
    }
    const line = lines[index];
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushList();
      const level = heading[1].length;
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
    if (bullet) {
      listItems.push(`<li>${inline(bullet[1])}</li>`);
      index += 1;
      continue;
    }
    flushList();
    output.push(`${inline(line)}<br>`);
    index += 1;
  }
  flushList();
  element.innerHTML = output.join("");
}

function addStoredMessage(message) {
  const group = document.createElement("div");
  group.className = `message-group ${message.role}`;
  group.dataset.messageId = message.id;
  const element = document.createElement("div");
  element.className = `message ${message.role}`;
  const content = document.createElement("span");
  if (message.role === "assistant") renderAssistantText(content, message.content);
  else content.textContent = getUserDisplayContent(message.content);
  element.append(content);
  group.appendChild(element);
  if (message.generated_file) addGeneratedFileLink(group, message.generated_file);
  if (message.role === "user") {
    const actions = document.createElement("span");
    actions.className = "message-actions";
    const copy = document.createElement("button");
    copy.className = "message-copy";
    copy.textContent = "▣";
    copy.title = "Copy message";
    copy.addEventListener("click", () => navigator.clipboard.writeText(message.content));
    const edit = document.createElement("button");
    edit.className = "message-edit";
    edit.textContent = "✎";
    edit.title = "Edit message";
    edit.addEventListener("click", () => editStoredMessage({ ...message, content: getUserDisplayContent(message.content).replace(/\n\n📎 .+$/, "") }, content));
    actions.append(copy, edit);
    group.appendChild(actions);
  }
  messages.appendChild(group);
  messages.scrollTop = messages.scrollHeight;
}

async function editStoredMessage(message, content) {
  const editedConversation = activeConversation;
  const group = content.closest(".message-group");
  const actions = group.querySelector(".message-actions");
  const editor = document.createElement("div");
  editor.className = "message-editor";
  editor.contentEditable = "true";
  editor.textContent = message.content;
  const save = document.createElement("button");
  save.className = "message-save";
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.className = "message-cancel";
  cancel.textContent = "Cancel";
  let editFinished = false;
  const finish = async (shouldSave) => {
    if (editFinished) return;

    if (!shouldSave) {
      editFinished = true;

      if (activeConversation?.id === editedConversation.id) {
        await selectConversation(editedConversation);
      }

      return;
    }

    const nextContent = editor.textContent.trim();
    if (!nextContent) return;
    editFinished = true;
    const messageGroups = [...messages.querySelectorAll(".message-group")];
    const currentIndex = messageGroups.indexOf(group);
    messageGroups.slice(currentIndex + 1).forEach((messageGroup) => messageGroup.remove());
    status.textContent = "Thinking...";
    editor.disabled = true;
    actions.remove();
    try {
      editor.replaceWith(content);
      content.textContent = nextContent;
      let assistantBubble = null;
      let reply = "";
      const response = await fetch(`/api/messages/${message.id}/stream`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: user.id, content: nextContent }) });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "Could not regenerate the message.");
      }
      const handleRegeneratedToken = (token) => {
        reply += token;

        if (activeConversation?.id !== editedConversation.id) return;

        if (!assistantBubble) {
          assistantBubble = addMessage("", "assistant");
        }

        renderAssistantText(assistantBubble, reply);
        messages.scrollTop = messages.scrollHeight;
      };
      await readTokenStream(response, handleRegeneratedToken);

      if (activeConversation?.id === editedConversation.id) {
        await selectConversation(editedConversation);
        status.textContent = "";
      }
    } catch (error) {
      if (activeConversation?.id === editedConversation.id) {
        await selectConversation(editedConversation);
        status.textContent = error.message;
      }
    }
  };
  save.addEventListener("click", () => finish(true));
  cancel.addEventListener("click", () => finish(false));
  editor.addEventListener("keydown", (event) => {
    if (event.key === "Escape") finish(false);
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) finish(true);
  });
  content.replaceWith(editor);
  actions.replaceChildren(save, cancel);
  editor.focus();
}

async function deleteConversation(conversation) {
  const response = await fetch(`/api/conversations/${conversation.id}?user_id=${user.id}`, { method: "DELETE" });
  if (!response.ok) return;
  localStorage.removeItem("biology_active_conversation");
  await loadConversations();
}

async function renameConversation(conversation) {
  const row = document.querySelector(`[data-conversation-id="${conversation.id}"]`);
  const button = row.querySelector(".conversation");
  const rename = row.querySelector(".conversation-rename");
  const input = document.createElement("input");
  input.className = "conversation-input";
  input.value = conversation.title;
  button.replaceWith(input);
  input.focus();
  input.select();
  const submit = document.createElement("button");
  submit.className = "conversation-submit";
  submit.textContent = "✓";
  submit.title = "Save conversation name";
  rename.replaceWith(submit);
  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    const title = input.value.trim();
    if (!save || !title) {
      input.replaceWith(button);
      submit.replaceWith(rename);
      return;
    }
    const response = await fetch(`/api/conversations/${conversation.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: user.id, title }) });
    if (!response.ok) {
      input.replaceWith(button);
      submit.replaceWith(rename);
      return;
    }
    const updated = await response.json();
    conversation.title = updated.title;
    button.textContent = updated.title;
    input.replaceWith(button);
    submit.replaceWith(rename);
    if (activeConversation?.id === conversation.id) document.querySelector("#chat-title").textContent = updated.title;
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") finish(true);
    if (event.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  submit.addEventListener("click", () => finish(true));
}

function renderConversations(conversations) {
  conversationList.replaceChildren();
  conversations.forEach((conversation) => {
    const wrapper = document.createElement("div");
    wrapper.className = `conversation-row ${conversation.id === activeConversation?.id ? "active" : ""}`;
    wrapper.dataset.conversationId = conversation.id;
    const button = document.createElement("button");
    button.className = "conversation";
    button.textContent = conversation.title;
    button.addEventListener("click", () => selectConversation(conversation));
    const rename = document.createElement("button");
    rename.className = "conversation-rename";
    rename.textContent = "✎";
    rename.title = "Rename chat";
    rename.addEventListener("click", () => renameConversation(conversation));
    const remove = document.createElement("button");
    remove.className = "conversation-delete";
    remove.textContent = "×";
    remove.title = "Delete chat";
    remove.addEventListener("click", () => deleteConversation(conversation));
    wrapper.append(button, rename, remove);
    conversationList.appendChild(wrapper);
  });
}

async function attachLibraryDocument(documentId) {
  if (!activeConversation) return;
  const response = await fetch(`/api/conversations/${activeConversation.id}/documents/${encodeURIComponent(documentId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: user.id }),
  });
  if (!response.ok) throw new Error("Could not attach the PDF.");
  await loadDocumentLibrary();
  await loadActiveDocuments();
}

async function detachLibraryDocument(documentId) {
  if (!activeConversation) return;
  const response = await fetch(`/api/conversations/${activeConversation.id}/documents/${encodeURIComponent(documentId)}?user_id=${user.id}`, { method: "DELETE" });
  if (!response.ok) throw new Error("Could not detach the PDF.");
  await loadDocumentLibrary();
  await loadActiveDocuments();
}

function renderDocumentLibrary(documents) {
  documentLibrary.replaceChildren();
  const activeDocumentIds = new Set(activeDocumentRecords.map((documentRecord) => documentRecord.id));
  documents.forEach((documentRecord) => {
    const row = document.createElement("div");
    row.className = "document-row";
    const identity = document.createElement("div");
    const name = document.createElement("div");
    name.className = "document-name";
    name.textContent = documentRecord.filename;
    name.title = documentRecord.filename;
    const metadata = document.createElement("div");
    metadata.className = `document-meta ${documentRecord.status !== "ready" ? "document-progress" : ""}`;
    metadata.textContent = documentRecord.status === "ready" ? `${documentRecord.analysis_mode} · ${documentRecord.page_count || 0} pages` : `${documentRecord.status} · ${documentRecord.progress_percent}%`;
    identity.append(name, metadata);
    const action = document.createElement("button");
    action.className = "document-action";
    const isActive = activeDocumentIds.has(documentRecord.id);
    action.textContent = isActive ? "Detach" : "Attach";
    action.addEventListener("click", () => (isActive ? detachLibraryDocument(documentRecord.id) : attachLibraryDocument(documentRecord.id)).catch((error) => { status.textContent = error.message; }));
    row.append(identity, action);
    documentLibrary.appendChild(row);
  });
}

async function loadDocumentLibrary() {
  const response = await fetch(`/api/users/${user.id}/documents`);
  if (!response.ok) throw new Error("Could not load the PDF library.");
  const payload = await response.json();
  renderDocumentLibrary(payload.documents);
  return payload.documents;
}

function renderActiveDocuments() {
  activeDocuments.replaceChildren();
  activeDocumentRecords.forEach((documentRecord) => {
    const chip = document.createElement("span");
    chip.className = "document-chip";
    const label = document.createElement("span");
    const progress = documentRecord.status === "ready" ? "" : ` · ${documentRecord.progress_percent}%`;
    label.textContent = `${documentRecord.filename}${progress}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "Detach PDF";
    remove.addEventListener("click", () => detachLibraryDocument(documentRecord.id).catch((error) => { status.textContent = error.message; }));
    chip.append(label, remove);
    activeDocuments.appendChild(chip);
  });
  updateFileGenerationAvailability();
}

async function loadActiveDocuments() {
  if (!activeConversation) {
    activeDocumentRecords = [];
    renderActiveDocuments();
    return [];
  }
  const requestedConversationId = activeConversation.id;
  const response = await fetch(`/api/conversations/${requestedConversationId}/documents?user_id=${user.id}`);
  if (!response.ok) throw new Error("Could not load attached PDFs.");
  const payload = await response.json();

  if (activeConversation?.id !== requestedConversationId) {
    return payload.documents;
  }

  activeDocumentRecords = payload.documents;
  renderActiveDocuments();
  return activeDocumentRecords;
}

function setComposerBusy(isBusy) {
  sendButton.disabled = isBusy;
  fileInput.disabled = isBusy;
}

function waitForNextPoll() {
  return new Promise((resolve) => window.setTimeout(resolve, 1200));
}

async function waitForAnswerJob(answerJobId, answerConversationId) {
  unresolvedAnswerJobsByConversation.set(answerConversationId, answerJobId);
  updateComposerAvailability();

  while (unresolvedAnswerJobsByConversation.get(answerConversationId) === answerJobId) {
    const response = await fetch(`/api/answer-jobs/${answerJobId}?user_id=${user.id}`);
    if (!response.ok) throw new Error("Could not check the document answer.");
    const answerJob = await response.json();
    const isViewingAnswerConversation = activeConversation?.id === answerConversationId;

    if (isViewingAnswerConversation) {
      await Promise.all([loadActiveDocuments(), loadDocumentLibrary()]);
      const processingDocument = activeDocumentRecords.find((documentRecord) => documentRecord.status !== "ready");
      status.textContent = processingDocument ? `Analyzing ${processingDocument.filename} · ${processingDocument.progress_percent}%` : "Preparing a grounded answer...";
    }

    if (answerJob.status === "completed") {
      unresolvedAnswerJobsByConversation.delete(answerConversationId);

      if (isViewingAnswerConversation) {
        await selectConversation(activeConversation);
      }

      return;
    }

    if (["failed", "cancelled"].includes(answerJob.status)) {
      unresolvedAnswerJobsByConversation.delete(answerConversationId);
      throw new Error(answerJob.last_error || "The document answer could not be completed.");
    }

    await waitForNextPoll();
  }
}

async function loadConversations() {
  const response = await fetch(`/api/users/${user.id}/conversations`);
  if (!response.ok) throw new Error("Could not load conversations.");
  const conversations = await response.json();
  if (!conversations.length) return createConversation();
  renderConversations(conversations);
  const savedId = Number(localStorage.getItem("biology_active_conversation"));
  await selectConversation(conversations.find((conversation) => conversation.id === savedId) || conversations[0]);
}

async function createConversation() {
  const response = await fetch("/api/conversations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: user.id, title: "New conversation" }) });
  if (!response.ok) throw new Error("Could not create conversation.");
  const conversation = await response.json();
  activeConversation = conversation;
  localStorage.setItem("biology_active_conversation", conversation.id);
  document.querySelector("#chat-title").textContent = conversation.title;
  messages.replaceChildren();
  addMessage("Ask anything — this chat will keep its own history ✨", "assistant");
  await loadConversations();
}

async function selectConversation(conversation) {
  activeConversation = conversation;
  updateComposerAvailability();
  localStorage.setItem("biology_active_conversation", conversation.id);
  document.querySelector("#chat-title").textContent = conversation.title;
  const response = await fetch(`/api/conversations/${conversation.id}/messages?user_id=${user.id}`);
  if (!response.ok) throw new Error("Could not load conversation history.");
  const history = await response.json();

  if (activeConversation?.id !== conversation.id) {
    return;
  }

  messages.replaceChildren();
  if (!history.length) addMessage("Ask anything — this chat will keep its own history ✨", "assistant");
  history.forEach(addStoredMessage);
  await loadActiveDocuments();
  await loadDocumentLibrary();

  if (activeConversation?.id !== conversation.id) {
    return;
  }

  const conversations = await fetch(`/api/users/${user.id}/conversations`).then((result) => result.json());
  renderConversations(conversations);
}

async function readTokenStream(response, onToken, onFile = () => {}, onAnswerJob = () => {}, onDocument = () => {}) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop();
    for (const event of events) {
      const line = event.split("\n").find((part) => part.startsWith("data: "));
      if (!line) continue;
      const payload = line.slice(6);
      if (payload === "[DONE]") continue;
      const data = JSON.parse(payload);
      if (data.error) throw new Error(data.error);
      if (data.token) onToken(data.token);
      if (data.file) onFile(data.file);
      if (data.answer_job) onAnswerJob(data.answer_job);
      if (data.document) onDocument(data.document);
    }
  }
}

function createChatRequestOptions(attachedFile, message, conversationId, shouldGenerateFile) {
  if (!attachedFile) {
    const requestBody = { user_id: user.id, conversation_id: conversationId, message, generate_file: shouldGenerateFile };
    return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody) };
  }

  const formData = new FormData();
  formData.append("user_id", user.id);
  formData.append("conversation_id", conversationId);
  formData.append("message", message);
  formData.append("file", attachedFile);
  formData.append("generate_file", String(shouldGenerateFile));
  return { method: "POST", body: formData };
}

document.querySelector("#new-chat").addEventListener("click", () => createConversation().catch((error) => { status.textContent = error.message; }));
document.querySelector("#refresh-documents").addEventListener("click", () => Promise.all([loadDocumentLibrary(), loadActiveDocuments()]).catch((error) => { status.textContent = error.message; }));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || !activeConversation) return;
  const activeConversationId = activeConversation.id;
  const hasUnresolvedAnswer = unresolvedAnswerJobsByConversation.has(activeConversationId);
  const hasPendingRequest = pendingConversationRequests.has(activeConversationId);

  if (hasUnresolvedAnswer || hasPendingRequest) return;

  const attachedFile = fileInput.files[0];
  const shouldGenerateFile = generateFile;
  addMessage(attachedFile ? `${message}\n\n📎 ${attachedFile.name}` : message, "user");
  input.value = "";
  fileInput.value = "";
  attachmentStatus.textContent = "";
  turnOffFileGeneration();
  updateFileGenerationAvailability();
  status.textContent = "Thinking...";
  const submittedConversationId = activeConversation.id;
  let queuedAnswerJob = null;
  pendingConversationRequests.add(submittedConversationId);
  updateComposerAvailability();

  try {
    const requestOptions = createChatRequestOptions(attachedFile, message, submittedConversationId, shouldGenerateFile);
    const response = await fetch(attachedFile ? "/api/chat/stream-with-file" : "/api/chat/stream", requestOptions);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `The backend returned an error (${response.status}).`);
    }
    let assistantBubble = null;
    let reply = "";
    const handleStreamedToken = (token) => {
      reply += token;

      if (activeConversation?.id !== submittedConversationId) return;

      if (!assistantBubble) {
        assistantBubble = addMessage("", "assistant");
      }

      renderAssistantText(assistantBubble, reply);
      messages.scrollTop = messages.scrollHeight;
    };
    const handleGeneratedFile = (generatedFile) => {
      if (activeConversation?.id !== submittedConversationId) return;

      if (!assistantBubble) {
        assistantBubble = addMessage("", "assistant");
      }

      const assistantMessageGroup = assistantBubble.closest(".message-group");
      addGeneratedFileLink(assistantMessageGroup, generatedFile);
    };
    const handleAnswerJob = (answerJob) => {
      queuedAnswerJob = answerJob;
    };
    const handleUploadedDocument = () => {
      if (activeConversation?.id === submittedConversationId) {
        status.textContent = "PDF saved. Starting analysis...";
      }
    };
    await readTokenStream(response, handleStreamedToken, handleGeneratedFile, handleAnswerJob, handleUploadedDocument);
    if (queuedAnswerJob) {
      await waitForAnswerJob(queuedAnswerJob.id, submittedConversationId);
    } else if (activeConversation?.id === submittedConversationId) {
      await selectConversation(activeConversation);
    }
  } catch (error) {
    if (unresolvedAnswerJobsByConversation.get(submittedConversationId) === queuedAnswerJob?.id) {
      unresolvedAnswerJobsByConversation.delete(submittedConversationId);
    }

    if (activeConversation?.id === submittedConversationId) {
      addMessage(error.message, "assistant");
    }
  } finally {
    pendingConversationRequests.delete(submittedConversationId);
    updateComposerAvailability();

    if (activeConversation?.id === submittedConversationId) {
      status.textContent = "";
    }

    input.focus();
  }
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

loadConversations().catch((error) => { status.textContent = error.message; });
