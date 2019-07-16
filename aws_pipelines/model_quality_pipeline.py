import os
from pathlib import Path

from dotenv import load_dotenv
from sagemaker.drift_check_baselines import DriftCheckBaselines
from sagemaker.model_metrics import MetricsSource, ModelMetrics
from sagemaker.model_monitor.dataset_format import DatasetFormat
from sagemaker.pipeline import PipelineModel
from sagemaker.sklearn.model import SKLearnModel
from sagemaker.tensorflow.model import TensorFlowModel
from sagemaker.transformer import Transformer
from sagemaker.workflow.check_job_config import CheckJobConfig
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.workflow.conditions import ConditionLessThanOrEqualTo
from sagemaker.workflow.fail_step import FailStep
from sagemaker.workflow.functions import Join, JsonGet
from sagemaker.workflow.model_step import ModelStep

# from sagemaker.workflow.parameters import ParameterFloat
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.pipeline_context import LocalPipelineSession, PipelineSession
from sagemaker.workflow.pipeline_definition_config import PipelineDefinitionConfig
from sagemaker.workflow.properties import PropertyFile
from sagemaker.workflow.quality_check_step import (
    ModelQualityCheckConfig,
    QualityCheckStep,
)
from sagemaker.workflow.steps import CacheConfig, ProcessingStep, TransformStep

from aws_pipelines.cond_register_pipeline import create_fail_step
from aws_pipelines.data_quality_pipeline import create_quality_check_step
from aws_pipelines.evaluate_pipeline import create_evaluation_step
from aws_pipelines.preprocessing_pipeline import (
    create_processor,
    define_parameters,
    define_processing_step,
)
from aws_pipelines.training_pipeline import (
    create_tensorflow_estimator,
    create_training_step,
)
from aws_pipelines.tuning_pipeline import create_tuning_step

# Load environment variables if not provided
load_dotenv()


def model_step(pipeline_model: PipelineModel = None, config: dict = None) -> ModelStep:
    """
    Create a ModelStep to create a SageMaker model.

    Parameters:
        pipeline_model (PipelineModel): The pipeline model object.
        config (dict): Configuration dictionary.

    Returns:
        ModelStep: The model step object.
    """
    return ModelStep(
        name="create-model",
        step_args=pipeline_model.create(instance_type=config["instance_type"]),
    )


def test_predictions_step(
    create_model_step: ModelStep = None,
    config: dict = None,
    s3_location: str = None,
    preprocessing_step: ProcessingStep = None,
    cache_config: CacheConfig = None,
) -> TransformStep:
    """
    Create a TransformStep to generate test predictions.

    Parameters:
        create_model_step (ModelStep): The model step object.
        config (dict): Configuration dictionary.
        s3_location (str): The base S3 location for storing pipeline outputs.
        preprocessing_step (ProcessingStep): The preprocessing step object.
        cache_config (CacheConfig): Cache configuration.

    Returns:
        TransformStep: The transform step object.
    """
    transformer = Transformer(
        model_name=create_model_step.properties.ModelName,
        instance_type=config["instance_type"],
        instance_count=1,
        strategy="MultiRecord",
        accept="text/csv",
        assemble_with="Line",
        output_path=f"{s3_location}/transform",
        sagemaker_session=config["session"],
    )

    return TransformStep(
        name="generate-test-predictions",
        step_args=transformer.transform(
            data=preprocessing_step.properties.ProcessingOutputConfig.Outputs["test-baseline"].S3Output.S3Uri,
            join_source="Input",  # Do not join with input
            split_type="Line",
            content_type="text/csv",
            output_filter="$[6, -1]",  # Include the ground truth and prediction column
        ),
        cache_config=cache_config,
    )


def create_model_quality_check_step(
    generate_test_predictions_step: TransformStep = None,
    config: dict = None,
    role: str = None,
    model_quality_location: str = None,
    model_package_group_name: str = None,
    cache_config: CacheConfig = None,
) -> QualityCheckStep:
    """
    Create a Quality Check Step for model quality baseline.

    Parameters:
        generate_test_predictions_step (TransformStep): The transform step for generating test predictions.
        config (dict): Configuration dictionary.
        role (str): IAM role for SageMaker.
        model_quality_location (str): S3 location for model quality output.
        model_package_group_name (str): Model package group name.
        cache_config (CacheConfig): Cache configuration.

    Returns:
        QualityCheckStep: The quality check step object.
    """

    return QualityCheckStep(
        name="generate-model-quality-baseline",
        check_job_config=CheckJobConfig(
            instance_type="ml.c5.xlarge",
            instance_count=1,
            volume_size_in_gb=20,
            sagemaker_session=config["session"],
            role=role,
        ),
        quality_check_config=ModelQualityCheckConfig(
            baseline_dataset=generate_test_predictions_step.properties.TransformOutput.S3OutputPath,
            dataset_format=DatasetFormat.csv(header=False),
            problem_type="Regression",
            ground_truth_attribute="_c0",  # Column index for ground truth in test-baseline.csv.out
            inference_attribute="_c1",  # Column index for predictions in test-baseline.csv.out
            output_s3_uri=model_quality_location,
        ),
        model_package_group_name=model_package_group_name,
        skip_check=True,  # Change to false if the baseline has already been created
        register_new_baseline=True,
        cache_config=cache_config,
    )


def create_model_quality_model_metrics(
    data_quality_baseline_step: QualityCheckStep = None, model_quality_baseline_step: QualityCheckStep = None
) -> ModelMetrics:
    """
    Create model metrics for model quality and data quality baselines.

    Parameters:
        data_quality_baseline_step (QualityCheckStep): The data quality baseline step.
        model_quality_baseline_step (QualityCheckStep): The model quality baseline step.

    Returns:
        ModelMetrics: The model metrics object.
    """
    return ModelMetrics(
        model_statistics=MetricsSource(
            s3_uri=model_quality_baseline_step.properties.CalculatedBaselineStatistics,
            content_type="application/json",
        ),
        model_constraints=MetricsSource(
            s3_uri=model_quality_baseline_step.properties.CalculatedBaselineConstraints,
            content_type="application/json",
        ),
        model_data_statistics=MetricsSource(
            s3_uri=data_quality_baseline_step.properties.CalculatedBaselineStatistics,
            content_type="application/json",
        ),
        model_data_constraints=MetricsSource(
            s3_uri=data_quality_baseline_step.properties.CalculatedBaselineConstraints,
            content_type="application/json",
        ),
    )


def create_model_quality_drift_check_baselines(
    data_quality_baseline_step: QualityCheckStep = None, model_quality_baseline_step: QualityCheckStep = None
) -> DriftCheckBaselines:
    """
    Create drift check baselines for model quality and data quality.

    Parameters:
        data_quality_baseline_step (QualityCheckStep): The data quality baseline step.
        model_quality_baseline_step (QualityCheckStep): The model quality baseline step.

    Returns:
        DriftCheckBaselines: The drift check baselines object.
    """
    return DriftCheckBaselines(
        model_statistics=MetricsSource(
            s3_uri=model_quality_baseline_step.properties.BaselineUsedForDriftCheckStatistics,
            content_type="application/json",
        ),
        model_constraints=MetricsSource(
            s3_uri=model_quality_baseline_step.properties.BaselineUsedForDriftCheckConstraints,
            content_type="application/json",
        ),
        model_data_statistics=MetricsSource(
            s3_uri=data_quality_baseline_step.properties.BaselineUsedForDriftCheckStatistics,
            content_type="application/json",
        ),
        model_data_constraints=MetricsSource(
            s3_uri=data_quality_baseline_step.properties.BaselineUsedForDriftCheckConstraints,
            content_type="application/json",
        ),
    )


def create_condition_step(
    mae_threshold: float = 3000.00,
    evaluate_model_step: ProcessingStep = None,
    create_model_step: ModelStep = None,
    generate_test_predictions_step: TransformStep = None,
    model_quality_baseline_step: QualityCheckStep = None,
    register_model_step: ModelStep = None,
    fail_step: FailStep = None,
) -> ConditionStep:
    """
    Create a Condition Step to check if model performance meets the required threshold.

    Parameters:
        mae_threshold (float): The MAE threshold for model evaluation.
        evaluate_model_step (ProcessingStep): The evaluation step.
        create_model_step (ModelStep): The model creation step.
        generate_test_predictions_step (TransformStep): The step to generate test predictions.
        model_quality_baseline_step (QualityCheckStep): The step to create model quality baseline.
        register_model_step (ModelStep): The model registration step.
        fail_step (FailStep): The failure step.

    Returns:
        ConditionStep: The condition step object.
    """
    evaluation_report = PropertyFile(name="evaluation-report", output_name="evaluation", path="evaluation.json")

    condition = ConditionLessThanOrEqualTo(
        left=JsonGet(
            step_name=evaluate_model_step.name,
            property_file=evaluation_report,
            json_path="metrics.mean_absolute_error.value",
        ),
        right=mae_threshold,
    )

    return ConditionStep(
        name="check-model-mae",
        conditions=[condition],
        if_steps=(
            [
                create_model_step,
                generate_test_predictions_step,
                model_quality_baseline_step,
                register_model_step,
            ]
        ),
        else_steps=[fail_step],
    )


def create_registration_step(
    model: PipelineModel = None,
    config: dict = None,
    model_package_group_name: str = None,
    model_metrics: ModelMetrics = None,
    drift_check_baselines: DriftCheckBaselines = None,
    approval_status: str = "Approved",
) -> ModelStep:
    """
    Create a Registration Step using the supplied parameters.

    Parameters:
        model (FrameworkModel): The model object to register.
        config (dict): Configuration dictionary.
        model_package_group_name (str): Name of the model package group.
        approval_status (str): Approval status for the model package. Default is "Approved".
        model_metrics (ModelMetrics): Model metrics for registration. Default is None.
        drift_check_baselines (DriftCheckBaselines): Drift check baselines. Default is None.

    Returns:
        ModelStep: The registration step object.
    """

    return ModelStep(
        name="register",
        step_args=model.register(
            model_package_group_name=model_package_group_name,
            approval_status=approval_status,
            model_metrics=model_metrics,
            drift_check_baselines=drift_check_baselines,
            content_types=["text/csv", "application/json"],
            response_types=["text/csv", "application/json"],
            inference_instances=[config["instance_type"]],
            transform_instances=[config["instance_type"]],
            framework_version=config["framework_version"],
        ),
    )


def create_data_quality_pipeline(
    role: str = None,
    bucket: str = None,
    local_mode: bool = False,
    s3_location: str = None,
    code_folder: str = None,
    comet_api_key: str = None,
    comet_project_name: str = None,
    use_tuning_step: bool = True,
    model_package_group_name=None,
    mae_threshold: float = 3000.00,
    data_quality_location: str = None,
    model_quality_location: str = None,
):
    """
    Create a registration pipeline with a conditional step to evaluate model performance.

    Parameters:
        role (str): IAM role for SageMaker.
        bucket (str): S3 bucket for storing pipeline artifacts.
        local_mode (bool): A flag indicating whether to run the pipeline in local mode. Defaults to False.
        s3_location (str): The base S3 location for storing pipeline outputs. If None, it will default to f"s3://{bucket}".
        code_folder (str): The folder path where the processing script is located. If None, it will default to "../src".
        comet_apy_key (str): The Comet API key for logging.
        comet_project_name (str): The Comet project name for logging.
        use_tuning_step (bool): Flag to indicate if the tuning step should be used.
        model_package_group_name (str): Name of the model package group.
        mae_threshold (float): The MAE threshold for model evaluation.
        data_quality_location (str): S3 location for data quality output.
        model_quality_location (str): S3 location for model quality baseline.

    Returns:
        Pipeline: The SageMaker pipeline object.
    """
    # Configure pipeline session
    pipeline_session = PipelineSession(default_bucket=bucket) if not local_mode else None

    if local_mode:
        config = {
            "session": LocalPipelineSession(default_bucket=bucket),
            "instance_type": "local",
            "image": None,
        }
    else:
        config = {
            "session": pipeline_session,
            "instance_type": "ml.m5.xlarge",
            "image": None,
        }

    # Define pipeline configuration and caching
    pipeline_definition_config = PipelineDefinitionConfig(use_custom_job_prefix=True)
    cache_config = CacheConfig(enable_caching=True, expire_after="15d")

    # Define parameters
    dataset_location = define_parameters(s3_location)

    # Create processor
    processor = create_processor(role, config)

    # Define processing step
    preprocessing_step = define_processing_step(processor, dataset_location, code_folder, s3_location, cache_config)

    # Create TensorFlow estimator
    estimator = create_tensorflow_estimator(config, role, code_folder, comet_api_key, comet_project_name)

    # Create training step
    train_model_step = create_training_step(estimator, preprocessing_step, cache_config)

    # Create tuning step
    tune_model_step = create_tuning_step(estimator, preprocessing_step, cache_config)

    # Select model assets based on whether tuning is used
    if use_tuning_step:
        model_assets = tune_model_step.get_top_model_s3_uri(
            top_k=0,
            s3_bucket=config["session"].default_bucket(),
        )
    else:
        model_assets = train_model_step.properties.ModelArtifacts.S3ModelArtifacts

    # Create evaluation step
    evaluate_model_step = create_evaluation_step(role, preprocessing_step, cache_config, code_folder, model_assets, config)

    config["framework_version"] = "2.12"

    # Create TensorFlow model
    tensorflow_model = TensorFlowModel(
        model_data=model_assets,
        framework_version=config["framework_version"],
        sagemaker_session=config["session"],
        role=role,
    )

    transformation_pipeline_model = Join(
        on="/",
        values=[
            preprocessing_step.properties.ProcessingOutputConfig.Outputs["model"].S3Output.S3Uri,
            "model.tar.gz",
        ],
    )

    preprocessing_model = SKLearnModel(
        model_data=transformation_pipeline_model,
        entry_point="preprocessing_component.py",
        source_dir=(code_folder / "pipeline").as_posix(),
        framework_version="1.2-1",
        sagemaker_session=config["session"],
        role=role,
    )

    postprocessing_model = SKLearnModel(
        model_data=transformation_pipeline_model,
        entry_point="postprocessing_component.py",
        source_dir=(code_folder / "pipeline").as_posix(),
        framework_version="1.2-1",
        sagemaker_session=config["session"],
        role=role,
    )

    pipeline_model = PipelineModel(
        name="inference-model",
        models=[preprocessing_model, tensorflow_model, postprocessing_model],
        sagemaker_session=config["session"],
        role=role,
    )

    data_quality_baseline_step = create_quality_check_step(
        preprocessing_step, config, role, data_quality_location, model_package_group_name, cache_config
    )

    create_model_step = model_step(pipeline_model, config)

    generate_test_predictions_step = test_predictions_step(
        create_model_step, config, s3_location, preprocessing_step, cache_config
    )

    model_quality_baseline_step = create_model_quality_check_step(
        generate_test_predictions_step, config, role, model_quality_location, model_package_group_name, cache_config
    )

    model_metrics = create_model_quality_model_metrics(data_quality_baseline_step, model_quality_baseline_step)

    drift_check_baselines = create_model_quality_drift_check_baselines(data_quality_baseline_step, model_quality_baseline_step)

    # Create registration step
    register_model_step = create_registration_step(
        pipeline_model,
        config,
        model_package_group_name,
        model_metrics,
        drift_check_baselines,
        approval_status="Approved",
    )

    fail_step = create_fail_step(mae_threshold)

    condition_step = create_condition_step(
        mae_threshold,
        evaluate_model_step,
        create_model_step,
        generate_test_predictions_step,
        model_quality_baseline_step,
        register_model_step,
        fail_step,
    )

    # Create and return the pipeline
    model_quality_pipeline = Pipeline(
        name="model-quality-pipeline",
        parameters=[dataset_location, mae_threshold],
        steps=[
            preprocessing_step,
            tune_model_step if use_tuning_step else train_model_step,
            evaluate_model_step,
            data_quality_baseline_step,
            condition_step,
        ],
        pipeline_definition_config=pipeline_definition_config,
        sagemaker_session=config["session"],
    )

    model_quality_pipeline.upsert(role_arn=role)

    print("Pipeline upserted successfully.")

    return model_quality_pipeline


if __name__ == "__main__":
    try:
        role = os.environ["ROLE"]
        bucket = os.environ.get("BUCKET", None)
        local_mode = os.environ.get("LOCAL_MODE", "False") == "True"
        s3_location = f"s3://{bucket}"
        code_folder = Path("../src")
        comet_api_key = os.environ["COMET_API_KEY"]
        comet_project_name = os.environ["COMET_PROJECT_NAME"]
        pipeline_model_package_group = os.environ["PIPELINE_MODEL_PACKAGE_GROUP"]
        use_tuning_step = os.environ.get("USE_TUNING_STEP", "True") == "True"
        # THRESHOLD = os.environ["MAE_THRESHOLD"]
        # mae_threshold = ParameterFloat(name="mae_threshold", default_value=float(THRESHOLD))
        mae_threshold = float(os.environ["MAE_THRESHOLD"])
        data_quality_location = f"{s3_location}/monitoring/data-quality-baseline"
        model_quality_location = f"{s3_location}/monitoring/model-quality-baseline"
        print(f"MAE_THRESHOLD: {mae_threshold}")
        print(f"LOCAL_MODE: {local_mode}")
        print(f"USE_TUNING_STEP: {use_tuning_step}")

        if not bucket:
            raise ValueError("S3 bucket must be specified")

        model_quality_pipeline = create_data_quality_pipeline(
            role,
            bucket,
            local_mode,
            s3_location,
            code_folder,
            comet_api_key,
            comet_project_name,
            use_tuning_step,
            pipeline_model_package_group,
            mae_threshold,
            data_quality_location,
            model_quality_location,
        )

        # Start the pipeline execution (if required)
        model_quality_pipeline.start()

    except KeyError as e:
        print(f"Environment variable not set: {e}")
    except ValueError as e:
        print(f"Value error: {e}")
    except ImportError as e:
        print(f"Import error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")