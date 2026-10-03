export type Resource = { id: string; name: string; [key: string]: any };
export type Child = {
  id: string;
  name: string;
  description: string;
  prompt: string;
  skills: string[];
  tools: string[];
  wiki: string[];
};
export type Agent = {
  id?: string;
  name: string;
  model: string;
  prompt: string;
  mode: "react" | "plan";
  skills: string[];
  tools: string[];
  wiki: string[];
  subs: Child[];
  published?: number;
};
export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "needs_input";
export type RunEvent = {
  seq: number;
  kind: string;
  instance?: string;
  [key: string]: any;
};
export type Citation = {
  id: string;
  document_id: string;
  file: string;
  start_line: number;
  end_line: number;
};
export type RunState = {
  id: string;
  status: RunStatus;
  input: string;
  output: string;
  error: string | null;
  session_id: string;
  resumable: boolean;
  events: RunEvent[];
  artifacts: string[];
};
export type RunResponse = {
  run_id: string;
  session_id: string;
  version: number;
  after?: number;
};
export type Message = {
  role: "user" | "assistant";
  text: string;
  run_id?: string;
  citations?: Citation[];
  artifacts?: string[];
};
