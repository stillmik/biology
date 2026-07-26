const user = JSON.parse(localStorage.getItem("biology_user") || "null");
if (!user) window.location.href = "/register.html";

const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const messages = document.querySelector("#messages");
const status = document.querySelector("#status");
const conversationList = document.querySelector("#conversations");
let activeConversation = null;
document.querySelector("#username").textContent = user ? `@${user.username}` : "";

function addMessage(text, role) {
  const group = document.createElement("div");
  group.className = `message-group ${role}`;
  const element = document.createElement("div");
  element.className = `message ${role}`;
  element.textContent = text;
  group.appendChild(element);
  messages.appendChild(group);
  messages.scrollTop = messages.scrollHeight;
}

function addStoredMessage(message) {
  const group = document.createElement("div");
  group.className = `message-group ${message.role}`;
  group.dataset.messageId = message.id;
  const element = document.createElement("div");
  element.className = `message ${message.role}`;
  const content = document.createElement("span");
  content.textContent = message.content;
  element.append(content);
  group.appendChild(element);
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
    edit.addEventListener("click", () => editStoredMessage(message, content));
    actions.append(copy, edit);
    group.appendChild(actions);
  }
  messages.appendChild(group);
  messages.scrollTop = messages.scrollHeight;
}

async function editStoredMessage(message, content) {
  const group = content.closest(".message-group");
  const bubble = content.closest(".message");
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
  const finish = async (shouldSave) => {
    if (!shouldSave) return selectConversation(activeConversation);
    const nextContent = editor.textContent.trim();
    if (!nextContent) return;
    const messageGroups = [...messages.querySelectorAll(".message-group")];
    const currentIndex = messageGroups.indexOf(group);
    messageGroups.slice(currentIndex + 1).forEach((messageGroup) => messageGroup.remove());
    status.textContent = "Thinking...";
    editor.disabled = true;
    actions.replaceChildren();
    actions.classList.add("message-thinking");
    actions.textContent = "Thinking...";
    const response = await fetch(`/api/messages/${message.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: user.id, content: nextContent }) });
    if (response.ok) {
      await selectConversation(activeConversation);
    } else {
      await selectConversation(activeConversation);
    }
    status.textContent = "";
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
  const input = document.createElement("input");
  input.className = "conversation-input";
  input.value = conversation.title;
  button.replaceWith(input);
  input.focus();
  input.select();
  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    const title = input.value.trim();
    if (!save || !title) {
      input.replaceWith(button);
      return;
    }
    const response = await fetch(`/api/conversations/${conversation.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: user.id, title }) });
    if (!response.ok) {
      input.replaceWith(button);
      return;
    }
    const updated = await response.json();
    conversation.title = updated.title;
    button.textContent = updated.title;
    input.replaceWith(button);
    if (activeConversation?.id === conversation.id) document.querySelector("#chat-title").textContent = updated.title;
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") finish(true);
    if (event.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
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
  localStorage.setItem("biology_active_conversation", conversation.id);
  document.querySelector("#chat-title").textContent = conversation.title;
  const response = await fetch(`/api/conversations/${conversation.id}/messages?user_id=${user.id}`);
  if (!response.ok) throw new Error("Could not load conversation history.");
  const history = await response.json();
  messages.replaceChildren();
  if (!history.length) addMessage("Ask anything — this chat will keep its own history ✨", "assistant");
  history.forEach(addStoredMessage);
  const conversations = await fetch(`/api/users/${user.id}/conversations`).then((result) => result.json());
  renderConversations(conversations);
}

document.querySelector("#new-chat").addEventListener("click", () => createConversation().catch((error) => { status.textContent = error.message; }));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || !activeConversation) return;
  addMessage(message, "user");
  input.value = "";
  status.textContent = "Thinking...";
  try {
    const response = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: user.id, conversation_id: activeConversation.id, message }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "The backend returned an error.");
    addMessage(data.reply, "assistant");
    await selectConversation(activeConversation);
  } catch (error) {
    addMessage(error.message, "assistant");
  } finally {
    status.textContent = "";
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
