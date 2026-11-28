import type { Editor } from "@tiptap/core";
import type { SelectionSurroundings } from "../types";
import { getDocumentHeadings } from "./documentCache";

export const CHAT_SELECTION_LIMIT = 4000;
const NEARBY_LIMIT = 400;

function clip(text: string, limit: number, fromEnd = false): string {
  const characters = Array.from(text.trim());
  return (fromEnd ? characters.slice(-limit) : characters.slice(0, limit)).join("");
}

export function captureSelectionSurroundings(editor: Editor): SelectionSurroundings {
  const { doc, selection: { $from, $to } } = editor.state;
  let heading = "";
  for (const item of getDocumentHeadings(doc)) {
    if (item.position > $from.pos) break;
    heading = item.text;
  }
  const beforeBlock = $from.depth ? doc.textBetween($from.before(1), $from.pos, "\n") : "";
  const afterBlock = $to.depth ? doc.textBetween($to.pos, $to.after(1), "\n") : "";
  const preceding = doc.maybeChild($from.index(0) - 1);
  const following = doc.maybeChild($to.indexAfter(0));
  const adjacent = (node: typeof preceding) => node?.type.name === "paragraph" ? node.textContent : "";
  return {
    heading: clip(heading, 120),
    before: clip([adjacent(preceding), beforeBlock].filter(Boolean).join("\n"), NEARBY_LIMIT, true),
    after: clip([afterBlock, adjacent(following)].filter(Boolean).join("\n"), NEARBY_LIMIT),
  };
}
