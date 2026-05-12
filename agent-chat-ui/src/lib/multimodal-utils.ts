import { ContentBlock } from "@langchain/core/messages";
import { toast } from "sonner";

const DOCX_MIME_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const PDF_MIME_TYPE = "application/pdf";
const DOCUMENT_MIME_TYPES = [
  PDF_MIME_TYPE,
  "application/msword",
  DOCX_MIME_TYPE,
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/rtf",
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/tab-separated-values",
  "text/html",
  "text/xml",
  "application/json",
  "application/xml",
];
const EXTENSION_MIME_TYPES: Record<string, string> = {
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".csv": "text/csv",
  ".tsv": "text/tab-separated-values",
  ".json": "application/json",
  ".xml": "application/xml",
  ".html": "text/html",
  ".htm": "text/html",
  ".rtf": "application/rtf",
  ".pdf": PDF_MIME_TYPE,
  ".doc": "application/msword",
  ".docx": DOCX_MIME_TYPE,
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
};

export function inferMimeType(fileName: string, mimeType: string): string {
  if (mimeType) return mimeType;
  const dotIndex = fileName.lastIndexOf(".");
  const extension = dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : "";
  return EXTENSION_MIME_TYPES[extension] ?? "";
}

// Returns a Promise of a typed multimodal block for images or PDFs
export async function fileToContentBlock(
  file: File,
): Promise<ContentBlock.Multimodal.Data> {
  const supportedImageTypes = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
  ];
  const supportedFileTypes = [...supportedImageTypes, ...DOCUMENT_MIME_TYPES];

  const mimeType = inferMimeType(file.name, file.type);

  if (!supportedFileTypes.includes(mimeType)) {
    toast.error(
      `Unsupported file type: ${file.type || file.name}. Supported types are: ${supportedFileTypes.join(", ")}`,
    );
    return Promise.reject(new Error(`Unsupported file type: ${file.type || file.name}`));
  }

  const data = await fileToBase64(file);

  if (supportedImageTypes.includes(mimeType)) {
    return {
      type: "image",
      mimeType,
      data,
      metadata: { name: file.name },
    };
  }

  // PDF / Word document
  return {
    type: "file",
    mimeType,
    data,
    metadata: { filename: file.name },
  };
}

// Helper to convert File to base64 string
export async function fileToBase64(file: File): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const result = reader.result as string;
      // Remove the data:...;base64, prefix
      resolve(result.split(",")[1]);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Type guard for Base64ContentBlock
export function isBase64ContentBlock(
  block: unknown,
): block is ContentBlock.Multimodal.Data {
  if (typeof block !== "object" || block === null || !("type" in block))
    return false;
  // file type (legacy)
  if (
    (block as { type: unknown }).type === "file" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    ((block as { mimeType: string }).mimeType.startsWith("image/") ||
      DOCUMENT_MIME_TYPES.includes((block as { mimeType: string }).mimeType))
  ) {
    return true;
  }
  // image type (new)
  if (
    (block as { type: unknown }).type === "image" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    (block as { mimeType: string }).mimeType.startsWith("image/")
  ) {
    return true;
  }
  return false;
}

export async function describeContentBlockForModel(
  block: ContentBlock.Multimodal.Data,
): Promise<string> {
  const metadata = block.metadata ?? {};
  const name =
    typeof metadata.filename === "string"
      ? metadata.filename
      : typeof metadata.name === "string"
        ? metadata.name
        : "uploaded-file";
  const mimeType = block.mimeType ?? "unknown type";

  if (block.type === "image") {
    const text = await imageContentBlockToText(block);
    return text ? `[Uploaded image: ${name} (${mimeType})]\n${text}` : "";
  }

  if (block.type !== "file") {
    return "";
  }

  if (mimeType === DOCX_MIME_TYPE) {
    const text = await docxContentBlockToText(block);
    return text
      ? `[Uploaded file: ${name} (${mimeType})]\n${text}`
      : "";
  }

  if (mimeType === PDF_MIME_TYPE) {
    const text = await pdfContentBlockToText(block);
    return text
      ? `[Uploaded file: ${name} (${mimeType})]\n${text}`
      : "";
  }

  return "";
}

async function pdfContentBlockToText(
  block: ContentBlock.Multimodal.Data,
): Promise<string> {
  if (typeof block.data !== "string" || !block.data) return "";
  try {
    const response = await fetch("/api/extract-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mimeType: block.mimeType,
        data: block.data,
      }),
    });

    if (!response.ok) {
      return "";
    }

    const result = (await response.json()) as { text?: unknown };
    return typeof result.text === "string" ? result.text.trim() : "";
  } catch {
    return "";
  }
}

async function imageContentBlockToText(
  block: ContentBlock.Multimodal.Data,
): Promise<string> {
  if (typeof block.data !== "string" || !block.data) return "";
  try {
    const response = await fetch("/api/extract-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mimeType: block.mimeType,
        data: block.data,
      }),
    });

    if (!response.ok) {
      return "";
    }

    const result = (await response.json()) as { text?: unknown };
    return typeof result.text === "string" ? result.text.trim() : "";
  } catch {
    return "";
  }
}

async function docxContentBlockToText(
  block: ContentBlock.Multimodal.Data,
): Promise<string> {
  if (typeof block.data !== "string" || !block.data) return "";
  try {
    const zip = await import("jszip");
    const archive = await zip.default.loadAsync(base64ToUint8Array(block.data));
    const documentXml = await archive.file("word/document.xml")?.async("text");
    if (!documentXml) return "";

    const parser = new DOMParser();
    const doc = parser.parseFromString(documentXml, "application/xml");
    const ns = {
      w: "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
      wp: "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
      pic: "http://schemas.openxmlformats.org/drawingml/2006/picture",
      a: "http://schemas.openxmlformats.org/drawingml/2006/main",
      v: "urn:schemas-microsoft-com:vml",
    };

    const body = doc.getElementsByTagNameNS(ns.w, "body")[0];
    if (!body) return "";

    const extractImageMarkers = (element: Element): string[] => {
      const markers: string[] = [];
      const pushMarker = (label: string | null | undefined) => {
        const normalized = (label ?? "").trim();
        markers.push(
          normalized ? `[Embedded image: ${normalized}]` : "[Embedded image]",
        );
      };

      Array.from(element.getElementsByTagNameNS(ns.wp, "docPr")).forEach((node) =>
        pushMarker(
          node.getAttribute("descr") ||
            node.getAttribute("title") ||
            node.getAttribute("name"),
        ),
      );
      Array.from(element.getElementsByTagNameNS(ns.pic, "cNvPr")).forEach((node) =>
        pushMarker(
          node.getAttribute("descr") ||
            node.getAttribute("title") ||
            node.getAttribute("name"),
        ),
      );
      Array.from(element.getElementsByTagNameNS(ns.v, "shape")).forEach((node) =>
        pushMarker(
          node.getAttribute("alt") ||
            node.getAttribute("title") ||
            node.getAttribute("id"),
        ),
      );
      if (
        markers.length === 0 &&
        (element.getElementsByTagNameNS(ns.a, "blip").length > 0 ||
          element.getElementsByTagNameNS(ns.v, "imagedata").length > 0)
      ) {
        markers.push("[Embedded image]");
      }
      return Array.from(new Set(markers));
    };

    const renderParagraph = (paragraph: Element): string[] => {
      const text = Array.from(paragraph.getElementsByTagNameNS(ns.w, "t"))
        .map((node) => node.textContent ?? "")
        .join("")
        .trim();
      const lines: string[] = [];
      if (text) lines.push(text);
      lines.push(...extractImageMarkers(paragraph));
      return lines;
    };

    const renderTable = (table: Element): string[] => {
      const lines = ["[Table]"];
      const rows = Array.from(table.getElementsByTagNameNS(ns.w, "tr"));
      for (const row of rows) {
        const cells = Array.from(row.getElementsByTagNameNS(ns.w, "tc")).map(
          (cell) => {
            const parts: string[] = [];
            Array.from(cell.children).forEach((child) => {
              if (child.localName === "p") {
                parts.push(...renderParagraph(child));
              }
            });
            return parts.join(" ").trim();
          },
        );
        if (cells.some(Boolean)) {
          lines.push(cells.join(" | "));
        }
      }
      return lines;
    };

    const parts: string[] = [];
    Array.from(body.children).forEach((child) => {
      if (child.localName === "p") {
        parts.push(...renderParagraph(child));
      } else if (child.localName === "tbl") {
        parts.push(...renderTable(child));
      }
    });

    return parts.filter(Boolean).join("\n").trim();
  } catch {
    return "";
  }
}

function base64ToUint8Array(data: string): Uint8Array {
  const binary = atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}
