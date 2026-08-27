// Bundled once via esbuild (see package.json's "build:editor" script) into
// static/js/editor-bundle.js and loaded as a plain <script> in draft.html —
// deliberately NOT loaded as separate runtime CDN imports. Multi-package
// CDN ESM loading for Tiptap (tried first, both esm.sh and jsDelivr's
// +esm endpoint) produces multiple copies of prosemirror-state/model at
// runtime ("Adding different instances of a keyed plugin"), since each
// package is transformed into its own bundle server-side without truly
// sharing the peer dependency. A real bundler resolves everything through
// one node_modules tree, so there's exactly one copy of ProseMirror no
// matter how many Tiptap extensions are pulled in.
import { Editor, Node, Extension, mergeAttributes } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import Paragraph from '@tiptap/extension-paragraph';
import { TextStyle, FontFamily, FontSize, LineHeight } from '@tiptap/extension-text-style';
import Color from '@tiptap/extension-color';
import Highlight from '@tiptap/extension-highlight';
import TextAlign from '@tiptap/extension-text-align';
import { Table } from '@tiptap/extension-table';
import TableRow from '@tiptap/extension-table-row';
import TableHeader from '@tiptap/extension-table-header';
import TableCell from '@tiptap/extension-table-cell';
import Link from '@tiptap/extension-link';
import ImageResize from 'tiptap-extension-resize-image';
import Typography from '@tiptap/extension-typography';
import Subscript from '@tiptap/extension-subscript';
import Superscript from '@tiptap/extension-superscript';

// Tiptap parses inserted/pasted HTML against the editor's own schema —
// tags with no matching node/mark extension (a plain <div>, tried first)
// get silently dropped rather than kept as unknown content. A real page
// break needs its own registered node so it survives insertContent() and
// round-trips back out through getHTML() as the same
// <div class="page-break"> the PDF's page-break-before CSS rule matches.
const PageBreak = Node.create({
    name: 'pageBreak',
    group: 'block',
    atom: true,
    parseHTML() {
        return [{ tag: 'div.page-break' }];
    },
    renderHTML({ HTMLAttributes }) {
        return ['div', mergeAttributes(HTMLAttributes, { class: 'page-break' })];
    },
    addCommands() {
        return {
            setPageBreak: () => ({ commands }) => commands.insertContent({ type: this.name }),
        };
    },
});

// Pasting from Word (or Word-derived HTML, e.g. Outlook/Google Docs "export
// as Word") carries Office's own markup alongside the real content:
// namespaced <o:p>/<w:*>/<v:*> tags, MSO conditional comments, and
// class="MsoNormal"/"MsoListParagraphCxSpFirst" etc. Unlike a plain <div>,
// nh3 (documents/sanitize.py) DOES let `class` through on p/div (it's an
// allowlisted attribute name with no value restriction — the sanitizer only
// blocks tag/style-based injection, not attribute-value content), so this
// cruft would otherwise survive every save indefinitely rather than being
// dropped by the schema like a genuinely unknown tag is. Cleaned up
// client-side, before Tiptap ever parses the pasted HTML into its schema,
// via the same transformPastedHTML extension hook Tiptap itself documents
// for exactly this (https://tiptap.dev/docs/editor/guide/custom-extensions#transform-pasted-html).
const WordPasteCleanup = Extension.create({
    name: 'wordPasteCleanup',
    transformPastedHTML(html) {
        return html
            // MSO conditional comments (<!--[if gte mso 9]>...<![endif]-->)
            // and any other HTML comment Word/Outlook/Google-Docs-as-Word
            // sprinkles through the markup.
            .replace(/<!--[\s\S]*?-->/g, '')
            // <o:p>, <w:...>, <v:...>, <m:...> — Office's own XML
            // namespaces leaking into the HTML clipboard format.
            .replace(/<\/?[a-z]+:[a-z][\w-]*(?:\s[^>]*)?>/gi, '')
            // class="MsoNormal", class="MsoListParagraphCxSpFirst", etc.
            .replace(/\sclass="[^"]*Mso[^"]*"/gi, '')
            .replace(/\sclass='[^']*Mso[^']*'/gi, '');
    },
});

// StarterKit's bundled Paragraph node (@tiptap/extension-paragraph) never
// declares a `class` node attribute — only `addOptions`/`parseHTML`/
// `renderHTML`, checked directly in node_modules. ProseMirror's
// NodeType.create() silently drops any attribute key not declared in the
// node's own schema, so every `updateAttributes('paragraph', { class: ... })`
// call the ribbon makes (Clause Indent, Section Heading, Preamble,
// Signatory Block) was a complete no-op — not even a transaction was
// recorded. Extending Paragraph to declare the attribute (and disabling
// StarterKit's copy below) is the actual fix; renderHTML doesn't need to
// change since Tiptap core already folds every declared attribute's own
// renderHTML output into the HTMLAttributes the node's existing
// renderHTML receives.
const StyledParagraph = Paragraph.extend({
    addAttributes() {
        return {
            class: {
                default: null,
                parseHTML: (element) => element.getAttribute('class'),
                renderHTML: (attributes) => (attributes.class ? { class: attributes.class } : {}),
            },
        };
    },
});

window.TiptapBundle = {
    Editor, StarterKit, TextStyle, FontFamily, FontSize, LineHeight, Color, Highlight, TextAlign,
    Table, TableRow, TableHeader, TableCell, Link, ImageResize, Typography, PageBreak, WordPasteCleanup,
    StyledParagraph, Subscript, Superscript,
};
