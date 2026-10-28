import { describe, expect, it } from 'vitest'
import {
  appliedWorkbookFingerprint,
  assessExistingTargets,
  buildWorkbookApplyRecord,
  buildWorkbookFingerprint,
  gateExistingTargetUpdates,
  validateTargetNames,
} from './targetAssessment'

describe('assessExistingTargets', () => {
  it('flags create vs update and workbook deltas', () => {
    const assessment = assessExistingTargets({
      processName: 'Business Process Catalog',
      projectName: 'Contoso-BPC',
      knownProcesses: [{ name: 'Business Process Catalog' }],
      knownProjects: [],
      templatePath: 'C:\\templates\\guide-v2.xlsx',
      workbookVersion: '2026.08',
      lastAppliedWorkbook: {
        fingerprint: buildWorkbookFingerprint({
          templatePath: 'C:\\templates\\guide-v1.xlsx',
          workbookVersion: '2026.07',
        }),
      },
    })

    expect(assessment.process.exists).toBe(true)
    expect(assessment.process.recommendedAction).toBe('update-from-workbook')
    expect(assessment.project.exists).toBe(false)
    expect(assessment.project.recommendedAction).toBe('create')
    expect(assessment.workbook.changed).toBe(true)
  })

  const assessWorkbook = (lastAppliedWorkbook, overrides = {}) =>
    assessExistingTargets({
      processName: 'Business Process Catalog',
      projectName: 'Contoso-BPC',
      knownProcesses: [{ name: 'Business Process Catalog' }],
      templatePath: '/BPC Files/2026-Q4/ADO template guideline.xlsx',
      workbookVersion: '2026-Q4',
      lastAppliedWorkbook,
      ...overrides,
    }).workbook

  it('matches the workbook a live runner run applied', () => {
    const record = buildWorkbookApplyRecord({
      templatePath: ' /BPC Files/2026-Q4/ADO template guideline.xlsx ',
      workbookVersion: '2026-Q4',
      runnerFingerprint: 'sha256:0123456789abcdef::2026-q4',
      appliedAt: '2026-10-27T12:00:00.000Z',
    })

    expect(record).toEqual({
      fingerprint: '/bpc files/2026-q4/ado template guideline.xlsx::2026-q4',
      runnerFingerprint: 'sha256:0123456789abcdef::2026-q4',
      templatePath: '/BPC Files/2026-Q4/ADO template guideline.xlsx',
      workbookVersion: '2026-Q4',
      appliedAt: '2026-10-27T12:00:00.000Z',
    })
    expect(assessWorkbook(record).changed).toBe(false)
    expect(assessWorkbook(record, { workbookVersion: '2027-Q1' }).changed).toBe(true)
  })

  it('compares older records that stored the runner hash by path and version', () => {
    const legacy = {
      fingerprint: 'sha256:0123456789abcdef::2026-q4',
      templatePath: '/BPC Files/2026-Q4/ADO template guideline.xlsx',
      workbookVersion: '2026-Q4',
    }

    expect(appliedWorkbookFingerprint(legacy)).toBe(
      '/bpc files/2026-q4/ado template guideline.xlsx::2026-q4',
    )
    expect(assessWorkbook(legacy).changed).toBe(false)
    expect(appliedWorkbookFingerprint({ fingerprint: 'sha256:0123::x', templatePath: '' })).toBe(
      'sha256:0123::x',
    )
    expect(appliedWorkbookFingerprint(null)).toBeNull()
  })
})

describe('gateExistingTargetUpdates', () => {
  const baseAssessment = assessExistingTargets({
    processName: 'Business Process Catalog',
    projectName: 'Contoso-BPC',
    knownProcesses: [{ name: 'Business Process Catalog' }],
    knownProjects: [{ name: 'Contoso-BPC' }],
    templatePath: 'guide.xlsx',
    workbookVersion: '1',
    lastAppliedWorkbook: null,
  })

  it('requires confirmation before updating existing process/project', () => {
    const gated = gateExistingTargetUpdates({
      targetStepIds: ['process', 'layout', 'project'],
      assessment: baseAssessment,
      updatePolicy: 'require-confirmation',
      confirmProcessUpdate: false,
      confirmProjectUpdate: false,
    })

    expect(gated.ok).toBe(false)
    expect(gated.errors.join(' ')).toMatch(/Confirm update/)
  })

  it('allows updates when confirmations are set', () => {
    const gated = gateExistingTargetUpdates({
      targetStepIds: ['process', 'layout', 'project'],
      assessment: baseAssessment,
      updatePolicy: 'require-confirmation',
      confirmProcessUpdate: true,
      confirmProjectUpdate: true,
    })

    expect(gated.ok).toBe(true)
  })

  it('skips existing targets when policy is skip-existing', () => {
    const gated = gateExistingTargetUpdates({
      targetStepIds: ['process', 'layout', 'project'],
      assessment: baseAssessment,
      updatePolicy: 'skip-existing',
    })

    expect(gated.ok).toBe(true)
    expect(gated.skippedStepIds).toEqual(['process', 'project'])
  })

  it('treats a backlog-only run as changing the existing process', () => {
    const gate = (updatePolicy, confirmProcessUpdate = false) =>
      gateExistingTargetUpdates({
        targetStepIds: ['backlog'],
        assessment: baseAssessment,
        updatePolicy,
        confirmProcessUpdate,
      })

    expect(gate('require-confirmation').errors).toEqual([
      'Process "Business Process Catalog" already exists. Confirm update before applying process/workbook changes.',
    ])
    expect(gate('require-confirmation', true).ok).toBe(true)
    expect(gate('create-only').errors).toEqual([
      'Process "Business Process Catalog" already exists and update policy is create-only.',
    ])
  })
})

describe('validateTargetNames', () => {
  it('requires typed process and project names', () => {
    const result = validateTargetNames({
      processName: '',
      projectName: '',
      assessment: assessExistingTargets({
        processName: '',
        projectName: '',
        knownProcesses: [],
        knownProjects: [],
        templatePath: 'a.xlsx',
      }),
      updatePolicy: 'always-update',
    })

    expect(result.ok).toBe(false)
    expect(result.errors.processName[0]).toMatch(/Enter the process name/)
    expect(result.errors.projectName[0]).toMatch(/Enter the project name/)
  })

  it('accepts new names under create policy', () => {
    const result = validateTargetNames({
      processName: 'Business Process Catalog',
      projectName: 'Contoso-BPC',
      assessment: assessExistingTargets({
        processName: 'Business Process Catalog',
        projectName: 'Contoso-BPC',
        knownProcesses: [],
        knownProjects: [],
        templatePath: 'a.xlsx',
      }),
      updatePolicy: 'require-confirmation',
    })

    expect(result.ok).toBe(true)
  })

  it('only applies the policy to targets the planned steps change', () => {
    const assessment = assessExistingTargets({
      processName: 'Business Process Catalog',
      projectName: 'Contoso-BPC',
      knownProcesses: [{ name: 'Business Process Catalog' }],
      knownProjects: [{ name: 'Contoso-BPC' }],
      templatePath: 'a.xlsx',
    })
    const validate = (targetStepIds) =>
      validateTargetNames({
        processName: 'Business Process Catalog',
        projectName: 'Contoso-BPC',
        assessment,
        updatePolicy: 'require-confirmation',
        targetStepIds,
      })

    expect(validate(['catalog', 'report']).ok).toBe(true)
    expect(validate(['layout']).errors.policy).toEqual([
      'Process "Business Process Catalog" already exists. Confirm update before the workflow can proceed.',
    ])
    expect(validate(['project', 'teams']).errors.policy).toEqual([
      'Project "Contoso-BPC" already exists. Confirm update before the workflow can proceed.',
    ])
    expect(validate(['backlog']).errors.policy).toEqual([
      'Process "Business Process Catalog" already exists. Confirm update before the workflow can proceed.',
    ])
    expect(validate(null).errors.policy).toHaveLength(2)
  })
})
