process remove_host_{{ pid }} {

    // Send POST request to platform
    {% include "post.txt" ignore missing %}

    tag { sample_id }

    input:
    set sample_id, file(fastq_pair) from {{ input_channel }}
    val bowtie2Index from IN_index_files

    output:
    set sample_id , file("${sample_id}*.headersRenamed_*.fq.gz") into {{ output_channel }}
    {% with task_name="remove_host" %}
    {%- include "compiler_channels.txt" ignore missing -%}
    {% endwith %}

    script:
    """
    bowtie2 -x ${bowtie2Index} -1 ${fastq_pair[0]} -2 ${fastq_pair[1]} -p $task.cpus > ${sample_id}.bam

    samtools view -buh -f 12 -o ${sample_id}_samtools.bam -@ 2 ${sample_id}.bam

    samtools fastq -1 ${sample_id}_unmapped_1.fq -2 ${sample_id}_unmapped_2.fq ${sample_id}_samtools.bam

    renamePE_samtoolsFASTQ.py -1 ${sample_id}_unmapped_1.fq -2 ${sample_id}_unmapped_2.fq

    gzip *.headersRenamed_*.fq
    """
}

{{ forks }}