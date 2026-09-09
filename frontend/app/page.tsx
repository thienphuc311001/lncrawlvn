import CtaStrip from '@/components/cta-strip';
import Features from '@/components/features';
import AppTabs from '@/components/app-tabs';
import JobConsole from '@/components/job-console';
import JobRunnerProvider from '@/components/job-runner';
import PageFoot from '@/components/page-foot';
import TopNav from '@/components/top-nav';

export default function Page() {
  return (
    <JobRunnerProvider>
      <TopNav />
      <main id="content">
        <AppTabs />
        <Features />
        <CtaStrip />
      </main>
      <PageFoot />
      <JobConsole />
    </JobRunnerProvider>
  );
}
