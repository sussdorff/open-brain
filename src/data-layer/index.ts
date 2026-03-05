export interface SearchParams {
  query?: string;
  limit?: number;
  offset?: number;
  project?: string;
  type?: string;
  obs_type?: string;
  dateStart?: string;
  dateEnd?: string;
  orderBy?: string;
  filePath?: string;
}

export interface TimelineParams {
  anchor?: number;
  query?: string;
  depth_before?: number;
  depth_after?: number;
  project?: string;
}

export interface SaveMemoryParams {
  text: string;
  type?: string;
  project?: string;
  title?: string;
}

export interface Memory {
  id: number;
  index_id: number;
  session_id: number | null;
  type: string;
  title: string | null;
  content: string;
  metadata: Record<string, unknown>;
  priority: number;
  stability: string;
  created_at: string;
  updated_at: string;
}

export interface DataLayer {
  search(params: SearchParams): Promise<{ results: Memory[]; total: number }>;
  timeline(
    params: TimelineParams
  ): Promise<{ results: Memory[]; anchor_id: number | null }>;
  getObservations(ids: number[]): Promise<Memory[]>;
  saveMemory(
    params: SaveMemoryParams
  ): Promise<{ id: number; message: string }>;
  searchByConcept(
    query: string,
    limit?: number,
    project?: string
  ): Promise<{ results: Memory[] }>;
  getContext(
    limit?: number,
    project?: string
  ): Promise<{ sessions: unknown[] }>;
  stats(): Promise<Record<string, unknown>>;
}
