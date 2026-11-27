const notes = [
  {
    id: "feynman",
    folder: "学习方法",
    title: "费曼学习法",
    summary: "试着用自己的话讲清楚一件事。",
    sections: [
      ["先讲一遍", "假设对方第一次听到这个概念。先用一句话说出它是什么，再用一个具体的例子解释它。"],
      ["回去补上卡住的地方", "讲到哪里卡住，就回去查哪里。把含糊的术语换成自己理解的说法，再讲一遍。"],
    ],
  },
  {
    id: "review",
    folder: "学习方法",
    title: "间隔复习",
    summary: "把回想拆到不同天，比一次读很久更容易发现忘了什么。",
    sections: [
      ["先回想，再翻笔记", "合上笔记，列出记得的要点。把想不起来的部分标出来，翻回去核对。"],
      ["让复习相隔几天", "从第二天、第四天、第八天开始试。容易忘的内容多练几次，已经熟悉的可以拉长间隔。"],
    ],
  },
  {
    id: "cards",
    folder: "笔记整理",
    title: "知识卡片",
    summary: "一张卡片只记一个自己能说清楚的想法。",
    sections: [
      ["把标题写成问题", "把「学习方法」改成「为什么合上笔记回想有帮助？」。下次翻到卡片时，就知道它要回答什么。"],
      ["留下出处", "给卡片加上书名、章节或文章链接。以后要核对细节时，可以直接回到原来的资料。"],
    ],
  },
];

const answers = {
  explain: {
    text: "找一个没接触过这个概念的人，用自己的话讲一遍。讲不清楚的地方，就是值得再读的地方。",
    noteId: "feynman",
    section: 1,
  },
  review: {
    text: "可以从第二天、第四天、第八天开始。每次先回想，再翻回笔记核对。熟悉之后，可以拉长间隔。",
    noteId: "review",
    section: 1,
  },
};

const noteList = document.querySelector("#note-list");
const notePath = document.querySelector("#note-path");
const noteTitle = document.querySelector("#note-title");
const noteSummary = document.querySelector("#note-summary");
const noteBody = document.querySelector("#note-body");
const status = document.querySelector("#demo-status");
const workbench = document.querySelector(".workbench");
const toolbar = document.querySelector(".demo-toolbar");
const modeButtons = [...toolbar.querySelectorAll("[data-mode]")];
const searchInput = document.querySelector("#note-search");
const searchStatus = document.querySelector("#search-status");
const searchResults = document.querySelector("#search-results");
const questionButtons = [...document.querySelectorAll("[data-question]")];
const answerText = document.querySelector("#answer-text");
const answerSource = document.querySelector("#answer-source");
const editButton = document.querySelector("#edit-note");
const editor = document.querySelector("#editor");
const editForm = document.querySelector("#edit-form");
const editText = document.querySelector("#edit-text");

let activeNote = notes[0];
let activeAnswer = answers.explain;

function makeButton(text, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function openNote(note, sourceSection) {
  activeNote = note;
  notePath.textContent = "学习笔记 / " + note.folder;
  noteTitle.textContent = note.title;
  noteSummary.textContent = note.summary;
  noteBody.replaceChildren();

  for (const [index, [title, text]] of note.sections.entries()) {
    const heading = document.createElement("h4");
    heading.id = "source-" + note.id + "-" + index;
    heading.tabIndex = -1;
    heading.textContent = title;
    heading.classList.toggle("source-highlight", index === sourceSection);

    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    noteBody.append(heading, paragraph);
  }

  for (const button of noteList.children) {
    button.setAttribute("aria-current", String(button.dataset.note === note.id));
  }
  status.textContent = "";
}

function searchNotes() {
  const query = searchInput.value.trim().toLocaleLowerCase("zh-CN");
  const matchingNotes = notes.filter(note => {
    const fields = [note.title, note.summary, ...note.sections.flat()];
    return fields.some(field => field.toLocaleLowerCase("zh-CN").includes(query));
  });

  searchStatus.textContent = query ? "找到 " + matchingNotes.length + " 篇笔记" : "按标题或正文查找";
  searchResults.replaceChildren(...matchingNotes.map(note => {
    const button = makeButton(note.title, "", () => {
      openNote(note);
      noteTitle.focus();
    });
    const summary = document.createElement("span");
    summary.textContent = note.summary;
    button.append(summary);
    return button;
  }));
}

function showMode(mode) {
  workbench.dataset.view = mode;
  for (const button of modeButtons) {
    const selected = button.dataset.mode === mode;
    button.setAttribute("aria-pressed", String(selected));
    document.getElementById(button.dataset.mode + "-panel").hidden = !selected;
  }
  editButton.hidden = mode !== "write";
  if (mode === "search") searchNotes();
}

function showAnswer(question) {
  activeAnswer = answers[question];
  const note = notes.find(item => item.id === activeAnswer.noteId);
  answerText.textContent = activeAnswer.text;
  answerSource.textContent = "引用 · " + note.title;
  for (const button of questionButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.question === question));
  }
}

for (const note of notes) {
  const button = makeButton(note.title, "note-link", () => {
    openNote(note);
    status.textContent = "已打开" + note.title;
  });
  button.dataset.note = note.id;
  noteList.append(button);
}

for (const button of modeButtons) {
  button.addEventListener("click", () => showMode(button.dataset.mode));
}

for (const link of document.querySelectorAll("[data-start-mode]")) {
  link.addEventListener("click", () => showMode(link.dataset.startMode));
}

for (const button of questionButtons) {
  button.addEventListener("click", () => showAnswer(button.dataset.question));
}

searchInput.addEventListener("input", searchNotes);

answerSource.addEventListener("click", () => {
  const note = notes.find(item => item.id === activeAnswer.noteId);
  openNote(note, activeAnswer.section);
  document.getElementById("source-" + note.id + "-" + activeAnswer.section).focus();
});

editButton.addEventListener("click", () => {
  editText.setCustomValidity("");
  editText.value = activeNote.sections[0][1];
  editor.showModal();
  editText.focus();
});

editText.addEventListener("input", () => editText.setCustomValidity(""));
document.querySelector("#cancel-edit").addEventListener("click", () => editor.close());
editor.addEventListener("close", () => editButton.focus({ preventScroll: true }));

editForm.addEventListener("submit", event => {
  event.preventDefault();
  const text = editText.value.trim();
  if (!text) {
    editText.setCustomValidity("请填写正文。");
    editText.reportValidity();
    return;
  }
  activeNote.sections[0][1] = text;
  openNote(activeNote);
  status.textContent = "示例笔记已保存。";
  editor.close();
});

openNote(activeNote);
showAnswer("explain");
showMode("write");
noteList.hidden = false;
toolbar.hidden = false;
