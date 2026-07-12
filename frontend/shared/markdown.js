/* Safe, dependency-free Markdown renderer for user-visible generated content.
 * Raw HTML is escaped first; only a small allow-list of Markdown constructs is emitted.
 */
window.SafeMarkdown = (() => {
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);

  function inline(value) {
    const code = [];
    let text = value.replace(/`([^`]+)`/g, (_, content) => {
      code.push(`<code>${content}</code>`);
      return `\u0000CODE${code.length - 1}\u0000`;
    });
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    return text.replace(/\u0000CODE(\d+)\u0000/g, (_, index) => code[Number(index)]);
  }

  function render(markdown) {
    const source = escapeHtml(markdown).replace(/\r\n?/g, "\n");
    const lines = source.split("\n");
    const output = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }

      if (/^```/.test(line)) {
        const language = line.slice(3).trim().replace(/[^a-zA-Z0-9_-]/g, "");
        const content = [];
        index += 1;
        while (index < lines.length && !/^```/.test(lines[index])) content.push(lines[index++]);
        if (index < lines.length) index += 1;
        output.push(`<pre><code${language ? ` class="language-${language}"` : ""}>${content.join("\n")}</code></pre>`);
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }
      if (/^\s*(---+|___+|\*\*\*+)\s*$/.test(line)) {
        output.push("<hr>"); index += 1; continue;
      }

      if (index + 1 < lines.length && line.includes("|") && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])) {
        const cells = row => row.replace(/^\s*\||\|\s*$/g, "").split("|").map(cell => cell.trim());
        const headers = cells(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) rows.push(cells(lines[index++]));
        output.push(`<div class="markdown-table"><table><thead><tr>${headers.map(cell => `<th>${inline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${headers.map((_, cellIndex) => `<td>${inline(row[cellIndex] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }

      if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const tag = ordered ? "ol" : "ul";
        const items = [];
        while (index < lines.length) {
          const match = lines[index].match(ordered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/);
          if (!match) break;
          items.push(`<li>${inline(match[1])}</li>`);
          index += 1;
        }
        output.push(`<${tag}>${items.join("")}</${tag}>`);
        continue;
      }

      if (/^&gt;\s?/.test(line)) {
        const quotes = [];
        while (index < lines.length && /^&gt;\s?/.test(lines[index])) quotes.push(lines[index++].replace(/^&gt;\s?/, ""));
        output.push(`<blockquote>${quotes.map(inline).join("<br>")}</blockquote>`);
        continue;
      }

      const paragraph = [line];
      index += 1;
      while (index < lines.length && lines[index].trim() &&
             !/^(#{1,6})\s+|^```|^\s*[-*+]\s+|^\s*\d+[.)]\s+|^&gt;\s?/.test(lines[index])) {
        paragraph.push(lines[index++]);
      }
      output.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
    }
    return output.join("\n");
  }

  return {render};
})();
