'use client';

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

const API_BASE = 'http://127.0.0.1:8000';

type TrackedJob = {
  /** Poll target for the console. */
  jobId: string;
  /** Short label shown in the console header. */
  label: string;
};

export type JobStatus = 'idle' | 'running' | 'done' | 'failed';

type JobRunnerValue = {
  /** Job the console is currently attached to (null → dock hidden). */
  job: TrackedJob | null;
  /** Latest status the console reported for the tracked job. */
  jobStatus: JobStatus;
  /** Console reports status transitions back through this setter. */
  setJobStatus: (status: JobStatus) => void;
  /** Bump to remount the console when the same job id is re-tracked. */
  jobKey: number;
  /** POST /api/extract and start tracking the new job. */
  startJob: (url: string) => Promise<void>;
  /** Attach the console to an existing job (e.g. fetch-missing). */
  trackJob: (jobId: string, label: string) => void;
  /** Hide the console. */
  clearJob: () => void;
};

const JobRunnerContext = createContext<JobRunnerValue | null>(null);

export function useJobRunner(): JobRunnerValue {
  const ctx = useContext(JobRunnerContext);
  if (!ctx) throw new Error('useJobRunner must be used inside <JobRunnerProvider>');
  return ctx;
}

export default function JobRunnerProvider({ children }: { children: ReactNode }) {
  const [job, setJob] = useState<TrackedJob | null>(null);
  const [jobKey, setJobKey] = useState(0);
  const [jobStatus, setJobStatus] = useState<JobStatus>('idle');
  const startSeq = useRef(0);

  const trackJob = useCallback((jobId: string, label: string) => {
    setJob({ jobId, label });
    setJobKey((key) => key + 1);
  }, []);

  const clearJob = useCallback(() => setJob(null), []);

  const startJob = useCallback(
    async (url: string) => {
      const seq = ++startSeq.current;
      const res = await fetch(`${API_BASE}/api/extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      if (!res.ok && res.status !== 202) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `API returned ${res.status}`);
      }
      const data = await res.json();
      // A newer startJob call won the race; drop this stale one.
      if (seq !== startSeq.current) return;
      trackJob(data.job_id, url);
    },
    [trackJob],
  );

  const value = useMemo(
    () => ({ job, jobStatus, setJobStatus, jobKey, startJob, trackJob, clearJob }),
    [job, jobStatus, jobKey, startJob, trackJob, clearJob],
  );

  return <JobRunnerContext.Provider value={value}>{children}</JobRunnerContext.Provider>;
}
